# NDIICAM — las salidas de vídeo de la GPU: el servicio es el dueño de la tarjeta
# y alquila cada pantalla al proceso que la pinta.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
"""En Linux solo un proceso manda en la tarjeta gráfica a la vez (el "maestro" DRM).
Hasta ahora el proceso que pintaba (receptor, cámara, pantalla de ayuda) la abría y
se quedaba con ella entera: valía para un monitor. Para usar varios a la vez, el
servicio se hace maestro y **alquila** (DRM lease) a cada proceso solo su pantalla:
el conector, su controlador (CRTC) y el plano primario. El alquiler es exclusivo
y caduca solo cuando el proceso cierra el descriptor, es decir, cuando muere.

Si la tarjeta no se deja (sin libdrm, sin permisos, un driver raro), no pasa nada:
el servicio no se hace maestro, los procesos abren la tarjeta como siempre y solo
hay un monitor.

Una pantalla recién enchufada no tiene modo puesto (con un maestro en marcha, la
consola del kernel no la configura): antes de alquilarla se le pone aquí su modo
preferido con un fotograma negro, para que kmssink la encuentre lista y use el
plano de vídeo, que es lo que escala sin gastar CPU."""
import ctypes as C
import fcntl
import glob
import os
import re
import struct
import threading

from nucleo import evento, leer

U32P, U64P = C.POINTER(C.c_uint32), C.POINTER(C.c_uint64)
DRM_MODE_OBJECT_PLANE = 0xeeeeeeee
DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_MODE_TYPE_PREFERRED = 1 << 3
DRM_MODE_CONNECTED = 1
TIPOS_CONECTOR = {1: "VGA", 2: "DVI-I", 3: "DVI-D", 4: "DVI-A", 5: "Composite", 6: "SVIDEO", 7: "LVDS",
                  8: "Component", 9: "DIN", 10: "DP", 11: "HDMI-A", 12: "HDMI-B", 13: "TV", 14: "eDP",
                  15: "Virtual", 16: "DSI", 17: "DPI", 18: "Writeback", 19: "SPI", 20: "USB"}
# ioctl DRM_IOCTL_MODE_CREATE_DUMB: _IOWR('d', 0xB2, struct drm_mode_create_dumb)
# (height, width, bpp, flags, handle, pitch: u32; size: u64 = 32 bytes)
CREAR_DUMB = (3 << 30) | (32 << 16) | (ord("d") << 8) | 0xB2


class ModeInfo(C.Structure):
    _fields_ = ([("clock", C.c_uint32)]
                + [(n, C.c_uint16) for n in ("hdisplay", "hsync_start", "hsync_end", "htotal", "hskew",
                                             "vdisplay", "vsync_start", "vsync_end", "vtotal", "vscan")]
                + [("vrefresh", C.c_uint32), ("flags", C.c_uint32), ("type", C.c_uint32), ("name", C.c_char * 32)])


class Res(C.Structure):
    _fields_ = [("count_fbs", C.c_int), ("fbs", U32P), ("count_crtcs", C.c_int), ("crtcs", U32P),
                ("count_connectors", C.c_int), ("connectors", U32P), ("count_encoders", C.c_int),
                ("encoders", U32P), ("min_width", C.c_uint32), ("max_width", C.c_uint32),
                ("min_height", C.c_uint32), ("max_height", C.c_uint32)]


class Connector(C.Structure):
    _fields_ = [("connector_id", C.c_uint32), ("encoder_id", C.c_uint32), ("connector_type", C.c_uint32),
                ("connector_type_id", C.c_uint32), ("connection", C.c_int), ("mmWidth", C.c_uint32),
                ("mmHeight", C.c_uint32), ("subpixel", C.c_int), ("count_modes", C.c_int),
                ("modes", C.POINTER(ModeInfo)), ("count_props", C.c_int), ("props", U32P),
                ("prop_values", U64P), ("count_encoders", C.c_int), ("encoders", U32P)]


class Encoder(C.Structure):
    _fields_ = [("encoder_id", C.c_uint32), ("encoder_type", C.c_uint32), ("crtc_id", C.c_uint32),
                ("possible_crtcs", C.c_uint32), ("possible_clones", C.c_uint32)]


class Crtc(C.Structure):
    _fields_ = [("crtc_id", C.c_uint32), ("buffer_id", C.c_uint32), ("x", C.c_uint32), ("y", C.c_uint32),
                ("width", C.c_uint32), ("height", C.c_uint32), ("mode_valid", C.c_int), ("mode", ModeInfo),
                ("gamma_size", C.c_int)]


class PlaneRes(C.Structure):
    _fields_ = [("count_planes", C.c_uint32), ("planes", U32P)]


class Plane(C.Structure):
    _fields_ = [("count_formats", C.c_uint32), ("formats", U32P), ("plane_id", C.c_uint32), ("crtc_id", C.c_uint32),
                ("fb_id", C.c_uint32), ("crtc_x", C.c_uint32), ("crtc_y", C.c_uint32), ("x", C.c_uint32),
                ("y", C.c_uint32), ("possible_crtcs", C.c_uint32), ("gamma_size", C.c_uint32)]


class ObjProps(C.Structure):
    _fields_ = [("count_props", C.c_uint32), ("props", U32P), ("prop_values", U64P)]


class PropEnum(C.Structure):
    _fields_ = [("value", C.c_uint64), ("name", C.c_char * 32)]


class Prop(C.Structure):
    _fields_ = [("prop_id", C.c_uint32), ("flags", C.c_uint32), ("name", C.c_char * 32), ("count_values", C.c_int),
                ("values", U64P), ("count_enums", C.c_int), ("enums", C.POINTER(PropEnum)), ("count_blobs", C.c_int),
                ("blob_ids", U32P)]


class Lessees(C.Structure):
    _fields_ = [("count", C.c_uint32)]      # seguido de count uint32 con los ids


FIRMAS = {
    "drmModeGetResources": (C.POINTER(Res), [C.c_int]), "drmModeFreeResources": (None, [C.c_void_p]),
    "drmModeGetConnector": (C.POINTER(Connector), [C.c_int, C.c_uint32]), "drmModeFreeConnector": (None, [C.c_void_p]),
    "drmModeGetEncoder": (C.POINTER(Encoder), [C.c_int, C.c_uint32]), "drmModeFreeEncoder": (None, [C.c_void_p]),
    "drmModeGetCrtc": (C.POINTER(Crtc), [C.c_int, C.c_uint32]), "drmModeFreeCrtc": (None, [C.c_void_p]),
    "drmModeSetCrtc": (C.c_int, [C.c_int, C.c_uint32, C.c_uint32, C.c_uint32, C.c_uint32, U32P, C.c_int, C.POINTER(ModeInfo)]),
    "drmModeGetPlaneResources": (C.POINTER(PlaneRes), [C.c_int]), "drmModeFreePlaneResources": (None, [C.c_void_p]),
    "drmModeGetPlane": (C.POINTER(Plane), [C.c_int, C.c_uint32]), "drmModeFreePlane": (None, [C.c_void_p]),
    "drmModeObjectGetProperties": (C.POINTER(ObjProps), [C.c_int, C.c_uint32, C.c_uint32]),
    "drmModeFreeObjectProperties": (None, [C.c_void_p]),
    "drmModeGetProperty": (C.POINTER(Prop), [C.c_int, C.c_uint32]), "drmModeFreeProperty": (None, [C.c_void_p]),
    "drmModeAddFB": (C.c_int, [C.c_int, C.c_uint32, C.c_uint32, C.c_uint8, C.c_uint8, C.c_uint32, C.c_uint32, U32P]),
    "drmModeCreateLease": (C.c_int, [C.c_int, U32P, C.c_int, C.c_int, U32P]),
    "drmModeRevokeLease": (C.c_int, [C.c_int, C.c_uint32]),
    "drmModeListLessees": (C.POINTER(Lessees), [C.c_int]), "drmFree": (None, [C.c_void_p]),
    "drmSetMaster": (C.c_int, [C.c_int]), "drmDropMaster": (C.c_int, [C.c_int]), "drmIsMaster": (C.c_int, [C.c_int]),
    "drmSetClientCap": (C.c_int, [C.c_int, C.c_uint64, C.c_uint64]),
}


def cargar_libdrm():
    lib = C.CDLL("libdrm.so.2", use_errno=True)
    for nombre, (res, args) in FIRMAS.items():
        f = getattr(lib, nombre)
        f.restype, f.argtypes = res, args
    return lib


def tarjeta_con_salidas():
    """/dev/dri/cardN que tiene conectores (la del render no los tiene)."""
    for ruta in sorted(glob.glob("/sys/class/drm/card*-*")):
        if os.path.exists(f"{ruta}/status"):
            return "/dev/dri/" + os.path.basename(ruta).split("-", 1)[0]
    return None


def elegir_crtc(conector, conectores, crtcs_libres):
    """El controlador para una pantalla: el que ya la lleva si no lo comparte
    con otra conectada; si no, uno libre de los que pueden con ella."""
    de_otras = {c["crtc"] for c in conectores if c["id"] != conector["id"] and c["conectado"] and c["crtc"]}
    if conector["crtc"] and conector["crtc"] not in de_otras:
        return conector["crtc"]
    for crtc in conector["posibles"]:
        if crtc in crtcs_libres and crtc not in de_otras:
            return crtc
    return None


def elegir_modo(modos, pedido=None):
    """El modo para una pantalla sin modo: el pedido al kernel (video=WxH) si lo
    tiene, si no el preferido del monitor, si no el primero."""
    if pedido:
        for m in modos:
            if (m["ancho"], m["alto"]) == pedido:
                return m
    return next((m for m in modos if m["preferido"]), modos[0] if modos else None)


class Tarjeta:
    """La tarjeta gráfica como maestro DRM, y los alquileres de sus pantallas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.drm = None
        self.fd = None
        self.fallo = None           # por qué no se alquilan pantallas (None: sí)
        self.fbs = {}               # crtc -> framebuffer negro puesto aquí
        self.alquilados = {}        # conector -> (lessee, crtc)

    # ------------------------------------------------------------- apertura
    def _abrir(self):
        if self.fd is not None or self.fallo:
            return self.fd is not None
        try:
            self.drm = cargar_libdrm()
            dev = tarjeta_con_salidas()
            if not dev:
                raise OSError("sin tarjeta con salidas")
            fd = os.open(dev, os.O_RDWR | os.O_CLOEXEC)
            if not self.drm.drmIsMaster(fd) and self.drm.drmSetMaster(fd) != 0:
                os.close(fd)
                raise OSError("otro proceso es el maestro de la tarjeta")
            self.drm.drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)
            self.fd = fd
            return True
        except (OSError, AttributeError) as ex:
            self.fallo = str(ex)
            evento("pantallas_sin_alquiler", detalle=self.fallo)
            return False

    def disponible(self):
        """Si las pantallas se alquilan (varias a la vez) o se abren como antes (una)."""
        with self.lock:
            return self._abrir()

    def cerrar(self):
        with self.lock:
            if self.fd is not None:
                self.drm.drmDropMaster(self.fd)
                os.close(self.fd)
                self.fd = None

    # ------------------------------------------------------------ inventario
    def _leer(self):
        """Conectores, controladores y planos primarios, tal como están ahora."""
        drm, fd = self.drm, self.fd
        r = drm.drmModeGetResources(fd)
        if not r:
            return [], [], {}
        res = r.contents
        crtcs = [res.crtcs[i] for i in range(res.count_crtcs)]
        conectores = []
        for i in range(res.count_connectors):
            c = drm.drmModeGetConnector(fd, res.connectors[i])
            if not c:
                continue
            con = c.contents
            posibles, actual = 0, 0
            for k in range(con.count_encoders):
                e = drm.drmModeGetEncoder(fd, con.encoders[k])
                if e:
                    posibles |= e.contents.possible_crtcs
                    if con.encoders[k] == con.encoder_id:
                        actual = e.contents.crtc_id
                    drm.drmModeFreeEncoder(e)
            modos = [{"ancho": con.modes[k].hdisplay, "alto": con.modes[k].vdisplay, "hz": con.modes[k].vrefresh,
                      "preferido": bool(con.modes[k].type & DRM_MODE_TYPE_PREFERRED), "indice": k}
                     for k in range(con.count_modes)]
            conectores.append({"id": con.connector_id, "conectado": con.connection == DRM_MODE_CONNECTED,
                               "nombre": f"{TIPOS_CONECTOR.get(con.connector_type, str(con.connector_type))}-{con.connector_type_id}",
                               "crtc": actual, "posibles": [crtcs[k] for k in range(len(crtcs)) if posibles & (1 << k)],
                               "modos": modos, "_ptr": c})
        drm.drmModeFreeResources(r)
        primarios = {}
        pr = drm.drmModeGetPlaneResources(fd)
        if pr:
            for i in range(pr.contents.count_planes):
                p = drm.drmModeGetPlane(fd, pr.contents.planes[i])
                if not p:
                    continue
                tipo = None
                props = drm.drmModeObjectGetProperties(fd, p.contents.plane_id, DRM_MODE_OBJECT_PLANE)
                if props:
                    for k in range(props.contents.count_props):
                        pp = drm.drmModeGetProperty(fd, props.contents.props[k])
                        if pp:
                            if pp.contents.name == b"type":
                                tipo = props.contents.prop_values[k]
                            drm.drmModeFreeProperty(pp)
                    drm.drmModeFreeObjectProperties(props)
                if tipo == 1:   # DRM_PLANE_TYPE_PRIMARY
                    for k in range(len(crtcs)):
                        if p.contents.possible_crtcs & (1 << k):
                            primarios.setdefault(crtcs[k], p.contents.plane_id)
                drm.drmModeFreePlane(p)
            drm.drmModeFreePlaneResources(pr)
        return conectores, crtcs, primarios

    def _liberar(self, conectores):
        for c in conectores:
            self.drm.drmModeFreeConnector(c.pop("_ptr"))

    def _modo_crtc(self, crtc):
        c = self.drm.drmModeGetCrtc(self.fd, crtc)
        if not c:
            return None
        try:
            m = c.contents
            return (m.mode.hdisplay, m.mode.vdisplay) if m.mode_valid else None
        finally:
            self.drm.drmModeFreeCrtc(c)

    def _lessees(self):
        lst = self.drm.drmModeListLessees(self.fd)
        if not lst:
            return set()
        try:
            n = lst.contents.count
            ids = C.cast(C.addressof(lst.contents) + 4, U32P)
            return {ids[i] for i in range(n)}
        finally:
            self.drm.drmFree(lst)

    # -------------------------------------------------------------- consultas
    def resolucion(self, conector_id):
        """[ancho, alto] a los que está esa pantalla, o None si no se sabe."""
        with self.lock:
            if not self._abrir():
                return None
            conectores, _, _ = self._leer()
            try:
                c = next((c for c in conectores if c["id"] == conector_id), None)
                if not c or not c["crtc"]:
                    return None
                t = self._modo_crtc(c["crtc"])
                return list(t) if t else None
            finally:
                self._liberar(conectores)

    # -------------------------------------------------------------- alquiler
    def _poner_modo(self, con, crtc):
        """Una pantalla sin modo: su modo preferido con un fotograma negro."""
        drm, fd = self.drm, self.fd
        m = re.search(r"\bvideo=(?:[\w-]+:)?(\d+)x(\d+)", leer("/proc/cmdline"))
        modo = elegir_modo(con["modos"], (int(m.group(1)), int(m.group(2))) if m else None)
        if modo is None:
            return False
        if crtc not in self.fbs:
            # un búfer "dumb" del tamaño del modo (XRGB, 32 bits), negro
            datos = struct.pack("IIIIIIQ", modo["alto"], modo["ancho"], 32, 0, 0, 0, 0)
            try:
                r = fcntl.ioctl(fd, CREAR_DUMB, datos)
            except OSError as ex:
                evento("pantalla_sin_modo", detalle=f"dumb: {ex}")
                return False
            _, _, _, _, handle, paso, _ = struct.unpack("IIIIIIQ", r)
            fb = C.c_uint32(0)
            if drm.drmModeAddFB(fd, modo["ancho"], modo["alto"], 24, 32, paso, handle, C.byref(fb)) != 0:
                evento("pantalla_sin_modo", detalle="addfb")
                return False
            self.fbs[crtc] = fb.value
        info = con["_ptr"].contents.modes[modo["indice"]]
        conn = (C.c_uint32 * 1)(con["id"])
        if drm.drmModeSetCrtc(fd, crtc, self.fbs[crtc], 0, 0, conn, 1, C.byref(info)) != 0:
            evento("pantalla_sin_modo", detalle=f"setcrtc {con['nombre']} {modo['ancho']}x{modo['alto']}")
            return False
        evento("pantalla_modo", pantalla=con["nombre"], modo=f"{modo['ancho']}x{modo['alto']}")
        return True

    def alquilar(self, conector_id):
        """Descriptor DRM alquilado para pintar en esa pantalla (conector, su CRTC y
        su plano primario), o None si no se puede (y entonces el proceso abre la
        tarjeta él mismo, como antes). El que lo recibe lo pasa al hijo y cierra
        su copia: el alquiler vive mientras el hijo lo tenga abierto."""
        with self.lock:
            if not self._abrir():
                return None
            conectores, crtcs, primarios = self._leer()
            try:
                con = next((c for c in conectores if c["id"] == conector_id), None)
                if con is None or not con["conectado"]:
                    return None
                vivos = self._lessees()
                self.alquilados = {k: v for k, v in self.alquilados.items() if v[0] in vivos}
                anterior = self.alquilados.get(conector_id)
                if anterior:
                    # el hijo de antes aún no ha soltado la pantalla (está muriendo): fuera
                    self.drm.drmModeRevokeLease(self.fd, anterior[0])
                    del self.alquilados[conector_id]
                ocupados = {v[1] for v in self.alquilados.values()}
                crtc = elegir_crtc(con, conectores, [c for c in crtcs if c not in ocupados])
                if crtc is None or crtc not in primarios:
                    evento("pantalla_sin_alquiler", pantalla=con["nombre"], detalle="sin controlador libre")
                    return None
                if not self._modo_crtc(crtc) or con["crtc"] != crtc:
                    if not self._poner_modo(con, crtc):
                        return None
                objetos = (C.c_uint32 * 3)(con["id"], crtc, primarios[crtc])
                lessee = C.c_uint32(0)
                fd = self.drm.drmModeCreateLease(self.fd, objetos, 3, os.O_CLOEXEC, C.byref(lessee))
                if fd < 0:
                    evento("pantalla_sin_alquiler", pantalla=con["nombre"], detalle=f"errno {-fd}")
                    return None
                self.alquilados[conector_id] = (lessee.value, crtc)
                return fd
            finally:
                self._liberar(conectores)
