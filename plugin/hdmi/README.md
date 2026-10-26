# HDMI DMA-BUF sink

`ndihdmisink` is a private backport of the GStreamer KMS sink from **1.26.10**,
built against GStreamer **1.24 or newer** and libdrm **2.4.108 or newer**.
It imports the `DMA_DRM` format and explicit DRM modifiers produced by the VA
decoder/postprocessor. Ubuntu 24.04's stock `kmssink` does not negotiate these
caps. The system plugin is neither overwritten nor replaced.

Upstream sources: https://github.com/GStreamer/gstreamer/tree/1.26.10/subprojects/gst-plugins-bad/sys/kms

Changes from upstream:

- Private plugin/element names `ndihdmikms` / `ndihdmisink`.
- Private GObject types and C symbols (`GstNdiKMS`, `gst_ndi_kms`, `GST_NDI_KMS`)
  so both sinks can coexist in one process.
- Property iteration uses the 1.24-compatible `GQuark`, `g_quark_to_string`
  and `gst_structure_foreach` APIs instead of the newer `GstIdStr` APIs.
- Small standalone `config.h` and build script; no other GStreamer backport.

These files retain upstream copyright notices and are LGPL licensed; see
`COPYING.LIB` and individual file headers. They are separate from NDIICAM's
Python application. No binary is tracked in the repository.

## Build and install

On the target Linux machine:

```sh
sudo apt-get install build-essential pkg-config libgstreamer-plugins-base1.0-dev libdrm-dev
sudo sh plugin/hdmi/build.sh /opt/ndiicam/gst/libgstndihdmikms.so
sudo systemctl restart ndiicam
```

`install.sh` also attempts this build, with CPU fallback if unavailable.
`fuente.py` loads this library explicitly from its own `gst/` directory, so no
global `GST_PLUGIN_PATH` or plugin-cache changes are needed. Rebuild on a new
architecture/runtime. For standalone inspection:

```sh
GST_PLUGIN_PATH=/opt/ndiicam/gst gst-inspect-1.0 ndihdmisink
```

See `docs/hdmi-gpu-j3355.md` for application conditions, measurements and rollback.
