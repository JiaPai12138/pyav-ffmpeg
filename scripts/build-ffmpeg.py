import argparse
import concurrent.futures
import glob
import hashlib
import os
import platform
import shutil
import subprocess

from cibuildpkg import Builder, Package, When, fetch, get_platform, log_group, run

plat = platform.system()
is_musllinux = plat == "Linux" and platform.libc_ver()[0] != "glibc"


def calculate_sha256(filename: str) -> str:
    sha256_hash = hashlib.sha256()
    with open(filename, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


library_group = []

gnutls_group = []

codec_group = [
    Package(
        name="libsvtav1",
        source_url="https://gitlab.com/AOMediaCodec/SVT-AV1/-/archive/v3.0.1/SVT-AV1-v3.0.1.tar.bz2",
        sha256="f1d1ad8db551cd84ab52ae579b0e5086d8a0b7e47aea440e75907242a51b4cb9",
        build_system="cmake",
    ),
    Package(
        name="x264",
        source_url="https://code.videolan.org/videolan/x264/-/archive/32c3b801191522961102d4bea292cdb61068d0dd/x264-32c3b801191522961102d4bea292cdb61068d0dd.tar.bz2",
        sha256="d7748f350127cea138ad97479c385c9a35a6f8527bc6ef7a52236777cf30b839",
        # assembly contains textrels which are not supported by musl
        build_arguments=["--disable-asm"] if is_musllinux else [],
        # parallel build runs out of memory on Windows
        build_parallel=plat != "Windows",
        when=When.community_only,
    ),
    Package(
        name="x265",
        source_url="https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.1.tar.gz",
        sha256="a31699c6a89806b74b0151e5e6a7df65de4b49050482fe5ebf8a4379d7af8f29",
        build_system="cmake",
        source_dir="source",
        when=When.community_only,
    ),
]

alsa_package = None

nvheaders_package = Package(
    name="nv-codec-headers",
    source_url="https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/n13.0.19.0.tar.gz",
    sha256="86d15d1a7c0ac73a0eafdfc57bebfeba7da8264595bf531cf4d8db1c22940116",
    build_system="make",
)

ffmpeg_package = Package(
    name="ffmpeg",
    source_url="https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz",
    sha256="733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1",
    build_arguments=[],
    build_parallel=plat != "Windows",
)


def download_and_verify_package(package: Package) -> None:
    tarball = os.path.join(
        os.path.abspath("source"),
        package.source_filename or package.source_url.split("/")[-1],
    )

    if not os.path.exists(tarball):
        try:
            fetch(package.source_url, tarball)
        except subprocess.CalledProcessError:
            pass

    if not os.path.exists(tarball):
        raise ValueError(f"tar bar doesn't exist: {tarball}")

    sha = calculate_sha256(tarball)
    if package.sha256 == sha:
        print(f"{package.name} tarball: hashes match")
    else:
        raise ValueError(
            f"sha256 hash of {package.name} tarball do not match!\nExpected: {package.sha256}\nGot: {sha}"
        )


def download_tars(packages: list[Package]) -> None:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_package = {
            executor.submit(download_and_verify_package, package): package.name
            for package in packages
        }

        for future in concurrent.futures.as_completed(future_to_package):
            name = future_to_package[future]
            try:
                future.result()
            except Exception as exc:
                print(f"{name} generated an exception: {exc}")
                raise


def main():
    parser = argparse.ArgumentParser("build-ffmpeg")
    parser.add_argument("destination")
    parser.add_argument("--community", action="store_true")

    args = parser.parse_args()

    dest_dir = args.destination
    community = args.community

    # Use ALSA only on Linux.
    use_alsa = plat == "Linux"

    # Use CUDA if supported.
    use_cuda = plat in {"Linux", "Windows"}

    # Use GnuTLS only on Linux, FFmpeg has native TLS backends for macOS and Windows.
    use_gnutls = plat == "Linux"

    output_dir = os.path.abspath("output")
    if plat == "Linux" and os.environ.get("CIBUILDWHEEL") == "1":
        output_dir = "/output"
    output_tarball = os.path.join(output_dir, f"ffmpeg-{get_platform()}.tar.gz")

    if os.path.exists(output_tarball):
        return

    builder = Builder(dest_dir=dest_dir)
    builder.create_directories()

    # install packages
    available_tools = set()
    if plat == "Windows":
        available_tools.update(["gperf", "nasm"])

        # print tool locations
        print("PATH", os.environ["PATH"])
        for tool in ["gcc", "g++", "curl", "gperf", "ld", "nasm", "pkg-config"]:
            run(["where", tool])

    with log_group("install python packages"):
        run(["pip", "install", "cmake==3.31.6", "meson", "ninja"])

    # build tools
    build_tools = []
    if "gperf" not in available_tools:
        build_tools.append(
            Package(
                name="gperf",
                source_url="http://ftp.gnu.org/pub/gnu/gperf/gperf-3.1.tar.gz",
                sha256="588546b945bba4b70b6a3a616e80b4ab466e3f33024a352fc2198112cdbb3ae2",
            )
        )

    if "nasm" not in available_tools and platform.machine() not in {"arm64", "aarch64"}:
        build_tools.append(
            Package(
                name="nasm",
                source_url="https://www.nasm.us/pub/nasm/releasebuilds/2.14.02/nasm-2.14.02.tar.bz2",
                sha256="34fd26c70a277a9fdd54cb5ecf389badedaf48047b269d1008fbc819b24e80bc",
            )
        )

    ffmpeg_package.build_arguments = [
        # "--enable-alsa" if use_alsa else "--disable-alsa",
        "--disable-doc",
        "--disable-libtheora",
        "--disable-libfreetype",
        "--disable-libfontconfig",
        "--disable-libbluray",
        "--disable-libopenjpeg",
        (
            "--enable-mediafoundation"
            if plat == "Windows"
            else "--disable-mediafoundation"
        ),
        # "--enable-gmp",
        # "--enable-gnutls" if use_gnutls else "--disable-gnutls",
        # "--enable-libaom",
        # "--enable-libdav1d",
        # "--enable-libmp3lame",
        # "--enable-libopencore-amrnb" if community else "--disable-libopencore-amrnb",
        # "--enable-libopencore-amrwb" if community else "--disable-libopencore-amrwb",
        # "--enable-libopus",
        # "--enable-libspeex",
        "--enable-libsvtav1",
        # "--enable-libsrt" if community else "--disable-libsrt",
        # "--enable-libtwolame",
        # "--enable-libvorbis",
        # "--enable-libvpx",
        # "--enable-libwebp",
        # "--enable-libxcb" if plat == "Linux" else "--disable-libxcb",
        # "--enable-libxml2" if community else "--disable-libxml2",
        # "--enable-lzma",
        "--enable-zlib",
        # "--enable-version3",
    ]

    if use_cuda:
        ffmpeg_package.build_arguments.extend(["--enable-nvenc", "--enable-nvdec"])

    if community:
        ffmpeg_package.build_arguments.extend(
            [
                "--enable-libx264",
                "--enable-libx265",
                # "--disable-libopenh264",
                "--enable-gpl",
            ]
        )
    else:
        ffmpeg_package.build_arguments.extend(
            # ["--enable-libopenh264", "--disable-libx264", "--enable-libfdk_aac"]
            [
                "--enable-libx264",
                "--enable-libx265",
                # "--disable-libopenh264",
                "--enable-gpl",
            ]
        )

    if plat == "Darwin":
        ffmpeg_package.build_arguments.extend(
            [
                "--enable-videotoolbox",
                "--enable-audiotoolbox",
                "--extra-ldflags=-Wl,-ld_classic",
            ]
        )

    ffmpeg_package.build_arguments.extend(
        [
            "--disable-encoder=avui,dca,mlp,opus,s302m,sonic,sonic_ls,truehd,vorbis",
            "--disable-decoder=sonic",
            "--disable-libjack",
            "--disable-indev=jack",
        ]
    )

    packages = library_group[:]
    if use_alsa:
        packages += []  # alsa_package
    if use_cuda:
        packages += [nvheaders_package]

    if use_gnutls:
        packages += gnutls_group
    packages += codec_group
    packages += [ffmpeg_package]

    filtered_packages = []
    for package in packages:
        if package.when == When.community_only and not community:
            continue
        if package.when == When.commercial_only and community:
            continue
        filtered_packages.append(package)

    download_tars(build_tools + filtered_packages)
    for tool in build_tools:
        builder.build(tool, for_builder=True)
    for package in filtered_packages:
        builder.build(package)

    if plat == "Windows":
        # fix .lib files being installed in the wrong directory
        for name in (
            "avcodec",
            "avdevice",
            "avfilter",
            "avformat",
            "avutil",
            "postproc",
            "swresample",
            "swscale",
        ):
            if os.path.exists(os.path.join(dest_dir, "bin", name + ".lib")):
                shutil.move(
                    os.path.join(dest_dir, "bin", name + ".lib"),
                    os.path.join(dest_dir, "lib"),
                )

        # copy some libraries provided by mingw
        mingw_bindir = os.path.dirname(
            subprocess.run(["where", "gcc"], check=True, stdout=subprocess.PIPE)
            .stdout.decode()
            .splitlines()[0]
            .strip()
        )
        for name in (
            "libgcc_s_seh-1.dll",
            "libiconv-2.dll",
            "libstdc++-6.dll",
            "libwinpthread-1.dll",
            "zlib1.dll",
        ):
            shutil.copy(os.path.join(mingw_bindir, name), os.path.join(dest_dir, "bin"))

    # find libraries
    if plat == "Darwin":
        libraries = glob.glob(os.path.join(dest_dir, "lib", "*.dylib"))
    elif plat == "Linux":
        libraries = glob.glob(os.path.join(dest_dir, "lib", "*.so"))
    elif plat == "Windows":
        libraries = glob.glob(os.path.join(dest_dir, "bin", "*.dll"))

    # strip libraries
    if plat == "Darwin":
        run(["strip", "-S"] + libraries)
        run(["otool", "-L"] + libraries)
    else:
        run(["strip", "-s"] + libraries)

    # build output tarball
    os.makedirs(output_dir, exist_ok=True)
    run(["tar", "czvf", output_tarball, "-C", dest_dir, "bin", "include", "lib"])


if __name__ == "__main__":
    main()
