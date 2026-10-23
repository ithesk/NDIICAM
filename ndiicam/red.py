# NDIICAM — WiFi, NetworkManager, WiFi de configuración con portal cautivo e IP fija.
# Copyright (C) 2026 Pablo Holguín. GPL-3.0-or-later: ver LICENSE.
import glob
import os
import re
import socket
import subprocess
import threading
import time

import yaml

from nucleo import (AP_IF, AP_IP, CICLO_EXCLUSIVO, ESPERA_AP, PAUSA_EXCLUSIVO,
    PERFIL_TEMPORAL, RUN, canal, dispositivo_por_defecto, escribir_privado, evento,
    interfaz_wifi, ip_de, latido, leer, nm_valores, partir, sh, cacheado, olvidar_cache)


# ----------------------------------------------------------------------- red

class Red:
    def __init__(self, config):
        self.config = config
        self.wifi = interfaz_wifi()
        self.hostapd = self.dnsmasq = None
        self.ap_activo = False
        self.ap_if = None
        self.modo_ap = None         # simultaneo | exclusivo
        self.simultaneo_falla = False
        self.ap_desde = 0.0
        self.ultimo_online = time.time()
        self.online_desde = None
        self.forzado_hasta = 0.0
        self.reintento_ap = 0.0
        self.pausa_ap_hasta = 0.0
        self.conectando = False
        self.conexion = None
        self.cambio_ip = None
        self.escaneo = []
        self._limpiar_ap()

    # --- estado

    @cacheado(3)     # estado de interfaces: se pide varias veces por ciclo
    def dispositivos(self):
        devs = []
        for linea in sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                         "dev"]).splitlines():
            p = partir(linea)
            if len(p) >= 4:
                devs.append({"dev": p[0], "tipo": p[1], "estado": p[2],
                             "conexion": p[3]})
        return devs

    def online(self):
        return any(d["estado"] == "connected" and d["tipo"] in ("wifi", "ethernet")
                   and d["dev"] != AP_IF for d in self.dispositivos())

    @cacheado(3)     # señal y enlace WiFi
    def info_wifi(self):
        if not self.wifi or (self.ap_activo and self.modo_ap == "exclusivo"):
            return None
        out = sh(["iw", "dev", self.wifi, "link"])
        if "Connected to" not in out:
            return None

        def g(rx, tipo=str):
            m = re.search(rx, out)
            return tipo(m.group(1).strip()) if m else None
        senal = g(r"signal: (-?\d+)", int)
        freq = g(r"freq: ([\d.]+)", float)
        return {"ssid": g(r"SSID: (.+)"), "senal_dbm": senal,
                "senal_pct": None if senal is None else max(0, min(100, 2 * (senal + 100))),
                "banda": "5" if freq and freq > 4000 else "2.4",
                "canal": canal(freq), "tx_mbit": g(r"tx bitrate: ([\d.]+)", float),
                "rx_mbit": g(r"rx bitrate: ([\d.]+)", float), "ip": ip_de(self.wifi)}

    def clientes_ap(self):
        if not self.ap_if:
            return 0
        return sh(["iw", "dev", self.ap_if, "station", "dump"]).count("Station ")

    # --- bucle: WiFi de configuración

    def bucle(self):
        while True:
            try:
                self._paso()
            except Exception as ex:
                evento("fallo_interno", detalle=str(ex))
            latido("red")
            time.sleep(5)

    def _paso(self):
        if not self.wifi:
            return
        ahora = time.time()
        online = self.online()
        forzado = ahora < self.forzado_hasta
        if online:
            self.ultimo_online = ahora
            if self.online_desde is None:
                self.online_desde = ahora
        else:
            self.online_desde = None
        if self.ap_activo:
            if self.hostapd.poll() is not None or self.dnsmasq.poll() is not None:
                evento("ap_caida")
                self.detener_ap()
            elif self.modo_ap == "simultaneo":
                if online and not forzado:
                    en_linea = ahora - self.online_desde
                    # dar tiempo a quien está en el portal para ver el resultado
                    if (en_linea > 30 and self.clientes_ap() == 0) or en_linea > 300:
                        self.detener_ap()
                        evento("ap_off_red")
                return
            else:
                # exclusivo: con el AP puesto no se pueden buscar redes, así que
                # cada cierto tiempo, si nadie lo usa, se suelta para intentarlo
                if (not forzado and self.clientes_ap() == 0
                        and ahora - self.ap_desde > CICLO_EXCLUSIVO):
                    self.detener_ap()
                    self.pausa_ap_hasta = ahora + PAUSA_EXCLUSIVO
                return
        if ahora < self.pausa_ap_hasta or self.conectando:
            return
        if forzado:
            self.iniciar_ap(solo_simultaneo=online)
        elif not online and ahora - self.ultimo_online > ESPERA_AP:
            self.iniciar_ap()

    def forzar_ap(self, minutos):
        """Activa la WiFi de configuración a mano. Con conexión, solo si la
        tarjeta admite el modo simultáneo (si no, se cortaría la red)."""
        if minutos and self.online() and self.simultaneo_falla:
            return False
        self.forzado_hasta = time.time() + 60 * minutos if minutos else 0.0
        return True

    def iniciar_ap(self, solo_simultaneo=False):
        if not self.wifi or time.time() < self.reintento_ap:
            return
        os.makedirs(RUN, mode=0o755, exist_ok=True)
        cfg = self.config.get()
        if not self.simultaneo_falla:
            if self._levantar_ap(AP_IF, cfg, crear=True):
                self.modo_ap = "simultaneo"
                return self._ap_listo(cfg)
            self.simultaneo_falla = True
            self.detener_ap()
            evento("ap_sin_simultaneo")
        if solo_simultaneo:
            return
        # exclusivo: buscar redes ahora, que luego la tarjeta estará ocupada
        self.redes(buscar=True)
        sh(["nmcli", "dev", "set", self.wifi, "managed", "no"])
        time.sleep(1)
        if self._levantar_ap(self.wifi, cfg, crear=False):
            self.modo_ap = "exclusivo"
            return self._ap_listo(cfg)
        self.detener_ap()
        self.reintento_ap = time.time() + 60
        evento("ap_fallo")

    def _ap_listo(self, cfg):
        self.ap_activo, self.ap_desde = True, time.time()
        evento("ap_activa", ssid=cfg["ap_ssid"], modo=self.modo_ap)

    def _levantar_ap(self, iface, cfg, crear):
        if crear:
            # MAC propia para ap0: la de la WiFi con el bit de administración local
            b = leer(f"/sys/class/net/{self.wifi}/address").split(":")
            b[0] = "%02x" % (int(b[0], 16) | 0x02)
            if subprocess.run(["iw", "dev", self.wifi, "interface", "add", AP_IF,
                               "type", "__ap", "addr", ":".join(b)],
                              capture_output=True).returncode:
                return False
            # ap0 no reenvía hacia la red del local (además de la regla del cortafuegos)
            sh(["sysctl", "-q", "-w", f"net.ipv4.conf.{AP_IF}.forwarding=0"])
        # mismo canal que la WiFi cliente si está en 2,4 GHz; si no, el 6
        w = self.info_wifi()
        ch = w["canal"] if w and w["banda"] == "2.4" and w["canal"] else 6
        escribir_privado(f"{RUN}/hostapd.conf", "\n".join([
            f"interface={iface}", "driver=nl80211", f"ssid={cfg['ap_ssid']}",
            "hw_mode=g", f"channel={ch}", f"country_code={cfg['pais']}",
            "ieee80211n=1", "wpa=2", "wpa_key_mgmt=WPA-PSK", "rsn_pairwise=CCMP",
            f"wpa_passphrase={cfg['ap_clave']}", ""]))
        escribir_privado(f"{RUN}/dnsmasq.conf", "\n".join([
            f"interface={iface}", "bind-interfaces", f"listen-address={AP_IP}",
            "no-resolv", "no-hosts", "dhcp-authoritative",
            "dhcp-range=10.42.0.10,10.42.0.100,255.255.255.0,10m",
            f"dhcp-option=3,{AP_IP}", f"dhcp-option=6,{AP_IP}",
            # RFC 8910: los móviles abren el portal directamente
            f"dhcp-option=114,http://{AP_IP}/",
            # portal cautivo: todos los nombres resuelven al panel
            f"address=/#/{AP_IP}",
            f"dhcp-leasefile={RUN}/dnsmasq.leases", f"pid-file={RUN}/dnsmasq.pid",
            ""]))
        self.ap_if = iface
        # lo que digan va al diario del servicio: si un móvil no consigue
        # asociarse o el DHCP falla, se ve en Sistema › Registro
        self.hostapd = subprocess.Popen(["hostapd", f"{RUN}/hostapd.conf"],
                                        stdout=subprocess.DEVNULL, stderr=None)
        time.sleep(3)
        if self.hostapd.poll() is not None:
            return False
        sh(["ip", "addr", "add", f"{AP_IP}/24", "dev", iface])
        self.dnsmasq = subprocess.Popen(["dnsmasq", "-k", "-C", f"{RUN}/dnsmasq.conf"],
                                        stdout=subprocess.DEVNULL, stderr=None)
        time.sleep(1)
        return self.dnsmasq.poll() is None

    def detener_ap(self):
        for proc in (self.dnsmasq, self.hostapd):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.wifi and self.ap_if == self.wifi:
            # modo exclusivo: devolver la tarjeta a NetworkManager
            sh(["ip", "addr", "flush", "dev", self.wifi])
            sh(["nmcli", "dev", "set", self.wifi, "managed", "yes"])
        self.hostapd = self.dnsmasq = self.ap_if = self.modo_ap = None
        self.ap_activo = False
        self._limpiar_ap()

    def _limpiar_ap(self):
        sh(["pkill", "-f", f"{RUN}/hostapd.conf"])
        sh(["pkill", "-f", f"{RUN}/dnsmasq.conf"])
        if os.path.exists(f"/sys/class/net/{AP_IF}"):
            sh(["iw", "dev", AP_IF, "del"])

    # --- redes WiFi

    def redes(self, buscar):
        if not self.wifi or (self.ap_activo and self.modo_ap == "exclusivo"):
            return self.escaneo     # la tarjeta está haciendo de AP
        out = sh(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ",
                  "dev", "wifi", "list", "ifname", self.wifi,
                  "--rescan", "yes" if buscar else "auto"], timeout=30)
        vistas = {}
        for linea in out.splitlines():
            p = partir(linea)
            if len(p) < 6 or not p[1]:
                continue
            senal = int(p[2] or 0)
            previa = vistas.get(p[1])
            if previa is None or senal > previa["senal"]:
                vistas[p[1]] = {"ssid": p[1], "senal": senal, "seguridad": p[3],
                                "canal": p[4], "banda": "5" if p[5].startswith("5") else "2.4",
                                "activa": p[0] == "*" or bool(previa and previa["activa"])}
            elif p[0] == "*":
                previa["activa"] = True
        self.escaneo = sorted(vistas.values(), key=lambda r: -r["senal"])
        return self.escaneo

    def guardadas(self, con_claves=False):
        res = []
        for linea in sh(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY,ACTIVE",
                         "con", "show"]).splitlines():
            p = partir(linea)
            if len(p) < 4 or p[1] != "802-11-wireless" or p[0] == PERFIL_TEMPORAL:
                continue
            ssid = partir(sh(["nmcli", "-g", "802-11-wireless.ssid", "con", "show",
                              p[0]]).strip())[0]
            r = {"id": p[0], "ssid": ssid or p[0], "prioridad": int(p[2] or 0),
                 "activa": p[3] == "yes"}
            if con_claves:
                r["clave"] = sh(["nmcli", "-s", "-g", "802-11-wireless-security.psk",
                                 "con", "show", p[0]]).strip()
                r["seguridad"] = sh(["nmcli", "-g", "802-11-wireless-security.key-mgmt",
                                     "con", "show", p[0]]).strip()
                r["ip"] = self.ip_perfil(p[0])
            res.append(r)
        return sorted(res, key=lambda r: -r["prioridad"])

    def olvidar(self, ident):
        """Quita un perfil WiFi guardado. Solo WiFi: el panel manda el nombre
        del perfil y uno de cable ("Wired connection 1") dejaría el equipo sin red."""
        if sh(["nmcli", "-g", "connection.type", "con", "show", ident]).strip() != "802-11-wireless":
            return False
        if ident.startswith("netplan-"):
            # las redes escritas a mano en netplan comparten definición: se
            # quitan del YAML y no con nmcli, que se llevaría las demás
            ssid = partir(sh(["nmcli", "-g", "802-11-wireless.ssid", "con", "show",
                              ident]).strip())[0]
            for ruta in sorted(glob.glob("/etc/netplan/*.yaml")):
                with open(ruta) as f:
                    d = yaml.safe_load(f) or {}
                wifis = (d.get("network") or {}).get("wifis") or {}
                for iface, defn in list(wifis.items()):
                    aps = (defn or {}).get("access-points") or {}
                    if ssid not in aps:
                        continue
                    del aps[ssid]
                    if not aps:
                        del wifis[iface]
                    if not wifis:
                        d["network"].pop("wifis", None)
                    escribir_privado(ruta + ".tmp",
                                     yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
                    os.replace(ruta + ".tmp", ruta)
                    sh(["netplan", "generate"])
                    sh(["nmcli", "con", "reload"])
                    return True
        return subprocess.run(["nmcli", "con", "delete", ident],
                              capture_output=True).returncode == 0

    def olvidar_todas(self):
        for p in self.guardadas():
            self.olvidar(p["id"])

    def anadir_perfil(self, ssid, clave, seguridad=None, prioridad=40, ip=None,
                      nombre=None, autoconectar=True):
        args = ["nmcli", "con", "add", "type", "wifi", "ifname", self.wifi,
                "con-name", nombre or ssid, "ssid", ssid,
                "connection.autoconnect", "yes" if autoconectar else "no",
                "connection.autoconnect-retries", "0",
                "connection.autoconnect-priority", str(prioridad)]
        if clave:
            args += ["wifi-sec.key-mgmt", seguridad or "wpa-psk", "wifi-sec.psk", clave]
        if ip and ip.get("metodo") == "manual":
            args += self._args_ip(ip)
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    def conectar(self, ssid, clave):
        if self.conectando or not self.wifi:
            return False
        self.conectando = True
        self.conexion = {"ssid": ssid, "estado": "conectando"}
        threading.Thread(target=self._conectar, args=(ssid, clave), daemon=True).start()
        return True

    def _conectar(self, ssid, clave):
        try:
            self._intentar_conexion(ssid, clave)
        except Exception as ex:
            self._resultado("error", motivo="generico", detalle=str(ex))
        finally:
            self.conectando = False

    def _intentar_conexion(self, ssid, clave):
        if self.ap_activo and self.modo_ap == "exclusivo":
            # la tarjeta es el AP: soltarlo para poder conectar
            self.detener_ap()
            time.sleep(4)
        # se prueba con un perfil temporal: si la clave está mal, el perfil que
        # ya hubiera guardado para esa red sigue intacto
        sh(["nmcli", "con", "delete", PERFIL_TEMPORAL])
        seguridad = next((r["seguridad"] for r in self.escaneo if r["ssid"] == ssid), None)
        if seguridad and not clave:
            return self._resultado("error", motivo="necesita_clave")
        solo_wpa3 = seguridad and "WPA3" in seguridad and "WPA2" not in seguridad
        r = self.anadir_perfil(ssid, clave, "sae" if solo_wpa3 else "wpa-psk",
                               nombre=PERFIL_TEMPORAL)
        if r.returncode:
            return self._resultado("error", motivo="perfil", detalle=r.stderr.strip())
        r = subprocess.run(["nmcli", "--wait", "45", "con", "up", PERFIL_TEMPORAL],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            sh(["nmcli", "con", "delete", PERFIL_TEMPORAL])
            if "Secrets were required" in r.stderr or "802-1x" in r.stderr:
                motivo = "clave"
            elif "No network with SSID" in r.stderr or "not found" in r.stderr:
                motivo = "no_encontrada"
            else:
                motivo = "generico"
            return self._resultado("error", motivo=motivo)
        for p in self.guardadas():
            if p["ssid"] == ssid and p["id"] != PERFIL_TEMPORAL:
                self.olvidar(p["id"])
        sh(["nmcli", "con", "modify", PERFIL_TEMPORAL, "connection.id", ssid])
        self._resultado("ok", ip=ip_de(self.wifi), equipo=socket.gethostname())

    def _resultado(self, estado, **datos):
        self.conexion = {"ssid": self.conexion["ssid"], "estado": estado, **datos}
        evento("wifi_ok" if estado == "ok" else "wifi_error",
               ssid=self.conexion["ssid"], **datos)

    # --- dirección IP

    def ip_perfil(self, perfil):
        out = sh(["nmcli", "-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                  "con", "show", perfil]).splitlines() + ["", "", "", ""]
        direccion = (out[1].split(",")[0].strip() if out[1] else "")
        return {"metodo": out[0].strip() or "auto",
                "direccion": direccion.split("/")[0] if direccion else "",
                "prefijo": int(direccion.split("/")[1]) if "/" in direccion else 24,
                "gateway": out[2].strip(), "dns": [d for d in re.split(r"[,\s]+", out[3]) if d]}

    def _en_uso(self, dev):
        actual = sh(["nmcli", "-g", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show",
                     dev]).splitlines() + ["", "", ""]
        direccion = (nm_valores(actual[0]) or [""])[0]
        return {"direccion": direccion.split("/")[0],
                "prefijo": int(direccion.split("/")[1]) if "/" in direccion else 24,
                "gateway": actual[1].strip(), "dns": nm_valores(actual[2])}

    @cacheado(10)     # ajustes de IP: cambian solo al tocarlos (se olvida al cambiarlos)
    def ip_config(self):
        """Ajuste de IP del perfil que da la ruta por defecto y valores en uso."""
        dev = dispositivo_por_defecto()
        if not dev or dev == AP_IF:
            return None
        perfil = sh(["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", dev]).strip()
        if not perfil:
            return None
        return {"perfil": perfil, "dispositivo": dev, **self.ip_perfil(perfil),
                "en_uso": self._en_uso(dev), "cambio": self.cambio_ip}

    @cacheado(10)     # perfil del cable
    def perfil_cable(self):
        """Dispositivo y perfil de NetworkManager del cable, esté o no enchufado:
        si no está conectado, el primer perfil de cable (NetworkManager lo crea
        solo al enchufar: "Wired connection 1", o el que dejó netplan)."""
        eth = next((d for d in self.dispositivos()
                    if d["tipo"] == "ethernet" and d["dev"] != AP_IF), None)
        if not eth:
            return None
        conectado = eth["estado"] == "connected"
        perfil = eth["conexion"] if conectado else ""
        if not perfil:
            for linea in sh(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "con", "show"]).splitlines():
                p = partir(linea)
                if len(p) >= 2 and p[1] == "802-3-ethernet" and (len(p) < 3 or p[2] in ("", eth["dev"])):
                    perfil = p[0]
                    break
        return {"dev": eth["dev"], "perfil": perfil or None, "conectado": conectado,
                "mac": leer(f"/sys/class/net/{eth['dev']}/address")}

    @cacheado(10)     # ajustes de IP del cable
    def ip_config_cable(self):
        """Como ip_config, pero del cable, aunque no esté enchufado (sin en_uso)."""
        c = self.perfil_cable()
        if not c or not c["perfil"]:
            return None
        res = {"perfil": c["perfil"], "dispositivo": c["dev"], "conectado": c["conectado"],
               **self.ip_perfil(c["perfil"])}
        if c["conectado"]:
            res["en_uso"] = self._en_uso(c["dev"])
        return res

    @staticmethod
    def _args_ip(ip):
        if ip["metodo"] == "manual":
            return ["ipv4.method", "manual", "ipv4.addresses",
                    f"{ip['direccion']}/{ip['prefijo']}", "ipv4.gateway", ip["gateway"],
                    "ipv4.dns", " ".join(ip["dns"])]
        return ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""]

    def cambiar_ip(self, nuevo, conexion="activa"):
        if self.cambio_ip and self.cambio_ip["estado"] == "aplicando":
            return False
        olvidar_cache(self)
        if conexion == "cable":
            cfg = self.ip_config_cable()
            if not cfg:
                return False
            if not cfg["conectado"]:
                # sin cable enchufado no hay nada que probar: se guarda en el perfil
                # y NetworkManager lo aplica al conectarlo
                sh(["nmcli", "con", "modify", cfg["perfil"]] + self._args_ip(nuevo))
                self.cambio_ip = {"estado": "ok", "pendiente": True, "perfil": cfg["perfil"]}
                evento("ip_guardada", perfil=cfg["perfil"], metodo=nuevo["metodo"])
                return True
        else:
            cfg = self.ip_config()
        if not cfg:
            return False
        self.cambio_ip = {"estado": "aplicando"}
        threading.Thread(target=self._cambiar_ip, args=(cfg, nuevo), daemon=True).start()
        return True

    def _cambiar_ip(self, cfg, nuevo):
        perfil, dev = cfg["perfil"], cfg["dispositivo"]
        previo = {k: cfg[k] for k in ("metodo", "direccion", "prefijo", "gateway", "dns")}

        def aplicar(ip):
            sh(["nmcli", "con", "modify", perfil] + self._args_ip(ip))
            sh(["nmcli", "--wait", "30", "con", "up", perfil], timeout=40)

        def funciona():
            # con conexión y la puerta de enlace respondiendo, el cambio vale
            for _ in range(10):
                time.sleep(2)
                gw = (nm_valores(sh(["nmcli", "-g", "IP4.GATEWAY", "dev", "show", dev]))
                      or [None])[0]
                if gw and "1 received" in sh(["ping", "-c1", "-W2", gw]):
                    return True
            return False

        try:
            aplicar(nuevo)
            olvidar_cache(self)
            if funciona():
                self.cambio_ip = {"estado": "ok", "direccion": ip_de(dev),
                                  "equipo": socket.gethostname()}
                evento("ip_cambiada", perfil=perfil, metodo=nuevo["metodo"], direccion=ip_de(dev))
                return
            aplicar(previo)
            self.cambio_ip = {"estado": "error", "motivo": "sin_conexion"}
            evento("ip_revertida", perfil=perfil)
        except Exception as ex:
            aplicar(previo)
            self.cambio_ip = {"estado": "error", "motivo": "generico", "detalle": str(ex)}
            evento("ip_revertida", perfil=perfil)
