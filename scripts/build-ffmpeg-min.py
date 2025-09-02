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

codec_group = []

alsa_package = None

nvheaders_package = Package(
    name="nv-codec-headers",
    source_url="https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/n13.0.19.0.tar.gz",
    sha256="86d15d1a7c0ac73a0eafdfc57bebfeba7da8264595bf531cf4d8db1c22940116",
    build_system="make",
)

ffmpeg_package = Package(
    name="ffmpeg",
    source_url="https://ffmpeg.org/releases/ffmpeg-8.0.tar.xz",
    sha256="b2751fccb6cc4c77708113cd78b561059b6fa904b24162fa0be2d60273d27b8e",
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

    dest_dir = os.path.abspath(args.destination)
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
        # "--enable-small",
        "--disable-programs",
        "--disable-ffmpeg",
        "--disable-ffplay",
        "--disable-ffprobe",
        "--disable-doc",
        "--disable-htmlpages",
        "--disable-manpages",
        "--disable-podpages",
        "--disable-txtpages",
        "--disable-version3",
        "--disable-libxml2",
        "--disable-lzma",  # or re-add xz package
        "--disable-libtheora",
        "--disable-libfreetype",
        "--disable-libfontconfig",
        "--disable-libbluray",
        "--disable-libopenjpeg",
        "--disable-mediafoundation",
        "--disable-gmp",
        "--disable-alsa",
        "--disable-gnutls",
        "--disable-libaom",
        "--disable-libdav1d",
        "--disable-libmp3lame",
        "--disable-libopencore-amrnb",
        "--disable-libopencore-amrwb",
        "--disable-libopus",
        "--disable-libspeex",
        "--disable-libsvtav1",
        "--disable-libsrt",
        "--disable-libtwolame",
        "--disable-libvorbis",
        "--disable-libvpx",
        "--disable-libwebp",
        "--disable-libopenh264",
        "--disable-libxcb",
        "--disable-zlib",
        "--disable-libx264",
        "--disable-libx265",
    ]

    if use_cuda:
        ffmpeg_package.build_arguments.extend(["--enable-nvenc", "--enable-nvdec"])

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
            "--disable-encoders",
            "--enable-encoder=h264_nvenc",
            "--disable-decoders",
            "--enable-decoder=h264,h264_cuvid",
            "--disable-libjack",
            "--disable-indevs",
            "--disable-outdevs",
            "--disable-bsfs",
            "--disable-muxers",
            "--disable-demuxers",
            "--disable-protocols",
            "--disable-filters",
            "--disable-debug",
        ]
    )

    packages = library_group[:]
    if use_alsa:
        packages += [alsa_package]
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
