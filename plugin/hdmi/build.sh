#!/bin/sh
# Build only the private HDMI sink; system GStreamer stays unchanged.
set -eu
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=${1:-"$src/libgstndihdmikms.so"}
pkg-config --atleast-version=1.24 gstreamer-video-1.0
pkg-config --atleast-version=2.4.108 libdrm
mkdir -p "$(dirname -- "$out")"
tmp="$out.tmp.$$"
trap 'rm -f "$tmp"' EXIT HUP INT TERM
${CC:-cc} -shared -fPIC -O2 -DHAVE_CONFIG_H \
  -Werror=implicit-function-declaration -Wno-deprecated-declarations \
  -Wl,-z,defs -I"$src" -o "$tmp" \
  "$src/gstkmsallocator.c" "$src/gstkmsbufferpool.c" "$src/gstkmsedid.c" \
  "$src/gstkmssink.c" "$src/gstkmsutils.c" \
  $(pkg-config --cflags --libs gstreamer-video-1.0 gstreamer-allocators-1.0 libdrm) -lm
mv -f "$tmp" "$out"
