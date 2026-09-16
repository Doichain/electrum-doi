# Windows build environment.
#
# The previous version pinned exact apt package versions on ubuntu:20.04. That is
# how upstream kept builds reproducible, and it is also why this stopped working:
# once a release leaves the standard archive, those exact versions are gone and
# `apt-get install` fails with exit code 100. The AppImage builder rotted the same
# way on ubuntu:16.04.
#
# Upstream solved it by pinning the *archive* instead of the versions: Debian's
# snapshot service keeps a dated copy of the whole archive, so `apt-get install
# git` resolves to the same package in a year's time without naming a version.
# See apt.sources.list (the timestamp) and apt.preferences (the pin).
FROM debian:trixie@sha256:13f29b6806e531c3ff3b565bb6eed73f2132506c8c9d41bb996065ca20fb27f2

# ca-certificates has to be there before the snapshot archive can be used (https)
RUN apt update -qq > /dev/null && apt install -qq --yes --no-install-recommends \
    ca-certificates

COPY apt.sources.list /etc/apt/sources.list
COPY apt.preferences /etc/apt/preferences.d/snapshot

ENV LC_ALL=C.UTF-8 LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

# i386 as well: the default build is win32, so mingw and wine both need it.
RUN dpkg --add-architecture i386 && \
    apt-get update -q && \
    apt-get install -qy --allow-downgrades \
        lsb-release \
        wget \
        gnupg2 \
        dirmngr \
        python3 \
        git \
        p7zip-full \
        make \
        cmake \
        pkgconf \
        mingw-w64 \
        mingw-w64-tools \
        autotools-dev \
        autoconf \
        autopoint \
        libtool \
        gettext \
        sudo \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

# Wine from winehq, pinned to one version for the codename of the base image.
RUN mkdir -p /etc/apt/keyrings && \
    DEBIAN_CODENAME=$(lsb_release --codename --short) && \
    WINEVERSION="11.0.0.0~${DEBIAN_CODENAME}-1" && \
    wget -nc https://dl.winehq.org/wine-builds/winehq.key && \
        echo "d965d646defe94b3dfba6d5b4406900ac6c81065428bf9d9303ad7a72ee8d1b8 winehq.key" | sha256sum -c - && \
        cat winehq.key | gpg --dearmor -o /etc/apt/keyrings/winehq.gpg && \
        echo "deb [signed-by=/etc/apt/keyrings/winehq.gpg] https://dl.winehq.org/wine-builds/debian/ ${DEBIAN_CODENAME} main" >> /etc/apt/sources.list.d/winehq.list && \
        rm winehq.key && \
    apt-get update -q && \
    apt-get install -qy --allow-downgrades \
        wine-stable-amd64:amd64=${WINEVERSION} \
        wine-stable-i386:i386=${WINEVERSION} \
        wine-stable:amd64=${WINEVERSION} \
        winehq-stable:amd64=${WINEVERSION} \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

# win-iconv, because Debian does not package it while Ubuntu did
# (win-iconv-mingw-w64-dev, which the previous Dockerfile installed). zbar's QR
# decoder includes <iconv.h>, so without it contrib/make_zbar.sh fails with
# "qrcode/qrdectxt.c:10:10: fatal error: iconv.h: No such file or directory".
#
# It is one C file and one header, so it is built here for both mingw sysroots
# rather than carried as a patch. Pinned to tag v0.0.10.
ARG WIN_ICONV_COMMIT=9d16924a97cffcda415dc28569c15d34a6dbf1d5
RUN git clone https://github.com/win-iconv/win-iconv.git /tmp/win-iconv && \
    cd /tmp/win-iconv && \
    git checkout -q "${WIN_ICONV_COMMIT}" && \
    for target in i686-w64-mingw32 x86_64-w64-mingw32; do \
        "${target}-gcc" -O2 -c win_iconv.c -o "win_iconv-${target}.o" && \
        "${target}-ar" rcs "libiconv-${target}.a" "win_iconv-${target}.o" && \
        install -m644 iconv.h "/usr/${target}/include/iconv.h" && \
        install -m644 "libiconv-${target}.a" "/usr/${target}/lib/libiconv.a" ; \
    done && \
    rm -rf /tmp/win-iconv

# build.sh expects the wine prefix here and the checkout mounted below it, so
# that the scripts can address it as c:/electrum-doi.
RUN mkdir --parents /opt/wine64/drive_c/electrum-doi
