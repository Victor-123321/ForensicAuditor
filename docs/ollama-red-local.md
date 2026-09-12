# Ollama compartido en la red local

Un solo equipo del equipo sirve el modelo (`qwen2.5:7b`) y los demás lo
usan como backend del agente. Así nadie más necesita GPU ni descargar
7 GB, y todos investigamos contra el mismo modelo.

- **Servidor** = el equipo con Ollama y la GPU.
- **Clientes** = el resto de laptops, que corren la API y la UI de este
  repo apuntando al servidor.

> ⚠️ **Ollama no tiene autenticación.** Cualquiera que alcance el puerto
> 11434 puede usar el modelo, descargar otros o borrarlos. Esto va **solo
> en la red local**, con la regla de firewall acotada a la subred, y
> nunca con el puerto reenviado desde el router hacia internet.

---

## 1. Servidor: poner Ollama a escuchar en la red

Por defecto Ollama solo escucha en `127.0.0.1`, así que desde otra
máquina no se ve. Hay que ponerle `OLLAMA_HOST=0.0.0.0` de forma
**persistente** (que sobreviva a un reinicio a mitad del hackathon).

### Windows

Variable de **sistema**, no de usuario — si Ollama corre como servicio,
solo ve las de sistema. En PowerShell **como administrador**:

```powershell
setx /m OLLAMA_HOST "0.0.0.0:11434"
```

Reinicia Ollama (sal del icono de la bandeja y vuelve a abrirlo, o
`Restart-Service ollama` si lo instalaste como servicio). Comprueba que
tomó la variable:

```powershell
[Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Machine")
```

### Linux (systemd)

```bash
sudo systemctl edit ollama.service
```

y en la sección `[Service]`:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
```

luego:

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama.service -p Environment   # verificación
```

### macOS

```bash
launchctl setenv OLLAMA_HOST 0.0.0.0
```

y reinicia la app de Ollama. `launchctl setenv` no sobrevive al
reinicio: si el equipo se reinicia, vuelve a ejecutarlo (o lanza el
servidor a mano con `OLLAMA_HOST=0.0.0.0 ollama serve`).

### El modelo

```bash
ollama list                 # ¿aparece qwen2.5:7b?
ollama pull qwen2.5:7b      # si no
```

---

## 2. Servidor: abrir el puerto solo a la red local

Primero averigua tu subred (la IP local y su máscara):

| SO | Comando |
|---|---|
| Windows | `ipconfig` |
| Linux | `ip addr` (o `hostname -I`) |
| macOS | `ifconfig \| grep "inet "` |

Si tu IP es `192.168.1.23/24`, tu subred es `192.168.1.0/24`. Usa esa
subred en la regla (ajusta si la tuya es `192.168.0.x`, `10.0.0.x`, etc.):

**Windows** (PowerShell como administrador):

```powershell
New-NetFirewallRule -DisplayName "Ollama LAN" -Direction Inbound `
  -Protocol TCP -LocalPort 11434 -Action Allow `
  -Profile Private -RemoteAddress 192.168.1.0/24
```

**Linux con ufw**:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 11434 proto tcp
```

**Linux con firewalld**:

```bash
sudo firewall-cmd --permanent --zone=home \
  --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="11434" protocol="tcp" accept'
sudo firewall-cmd --reload
```

**macOS**: el firewall es por aplicación; si está activo, permite
entrada para `ollama` cuando lo pregunte.

---

## 3. Servidor: que no se suspenda a media demo

Un equipo suspendido tira todas las conexiones y el agente se queda
esperando. Con el cargador puesto:

```powershell
powercfg /change standby-timeout-ac 0     # Windows
```

```bash
sudo systemctl mask sleep.target suspend.target    # Linux
caffeinate -dims &                                  # macOS (mientras dure)
```

En Windows conviene también desactivar la suspensión selectiva del
adaptador de red, y **usar cable** si hay: el wifi de un evento es lo
primero que falla.

---

## 4. Servidor: verificar y repartir los datos

```bash
curl http://localhost:11434/api/tags      # ¿responde y sale qwen2.5:7b?
```

Y lo que hay que pasarle al resto del equipo:

```ini
OLLAMA_URL=http://<IP-DEL-SERVIDOR>:11434
OLLAMA_MODEL=qwen2.5:7b
```

(El cliente también acepta `http://<IP>:11434/api/generate`, `<IP>:11434`
o solo `<IP>`: normaliza la URL antes de usarla.)

---

## 5. Clientes: apuntar al servidor

Dos formas, la primera gana sobre la segunda:

1. **`.env`** en la raíz del repo (copia `.env.example`). Es lo cómodo
   para repartir por el chat del equipo:

   ```ini
   OLLAMA_URL=http://192.168.1.50:11434
   OLLAMA_MODEL=qwen2.5:7b
   ```

2. **Panel "Modelo local (Ollama)"** en la barra lateral de la UI:
   escribe la IP, pulsa **Buscar modelos**, elige el modelo y **Guardar**
   (queda en `~/.forensic_auditor/config.json`).

Comprueba la conexión sin abrir la UI:

```bash
python -m scripts.check_ollama                      # usa tu configuración
python -m scripts.check_ollama 192.168.1.50         # o un servidor concreto
```

---

## 6. Cuando no conecta

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `No hay nadie escuchando en http://IP:11434` | Ollama escucha solo en localhost, o el firewall bloquea | Pasos 1 y 2. Desde el **servidor**, `curl http://<su-propia-IP>:11434/api/tags`: si desde ahí sí responde pero desde fuera no, es el firewall |
| Funciona con `localhost` en el servidor pero no desde otra laptop | `OLLAMA_HOST` sin aplicar (variable de usuario en vez de sistema, o Ollama sin reiniciar) | Paso 1, y reinicia Ollama |
| Conecta, pero `model 'X' not found` | El modelo no está descargado en el servidor | `ollama pull qwen2.5:7b` |
| `no respondió en 300s` en la primera pregunta y luego va bien | Arranque en frío del modelo | Normal. Sube `OLLAMA_TIMEOUT` y deja `OLLAMA_KEEP_ALIVE=30m` para no repetirlo |
| Iba bien y de pronto deja de conectar | El servidor se suspendió, o cambió de IP | Paso 3; pide reserva DHCP en el router o usa el nombre `.local` |
| El `ping` falla | **No prueba nada por sí solo** | Muchas redes bloquean ICMP y aun así enrutan TCP. Comprueba con `python -m scripts.check_ollama <IP>`, nunca con ping |
| Ni el ping ni el 11434 pasan, y el servidor está bien configurado | **Aislamiento de clientes** en el wifi | Ver abajo |

### Aislamiento de clientes

Muchas redes de invitados y de eventos (y casi todos los wifis de
hoteles y campus) aíslan a los clientes entre sí: cada dispositivo llega
a internet pero no ve a los demás. Con eso, **nada de lo anterior va a
funcionar**, por bien configurado que esté el servidor.

**No uses `ping` para decidirlo.** Medido en la red del Tec: el ping al
servidor se pierde al 100% y aun así el puerto 11434 conecta y el modelo
responde. Muchísimas redes bloquean ICMP mientras enrutan TCP sin
problema, así que un ping fallido no dice nada. La única prueba que vale
es intentar hablar con el puerto:

```bash
python -m scripts.check_ollama <IP-DEL-SERVIDOR>
```

Solo si **eso** falla (y en el servidor `curl http://localhost:11434/api/tags`
sí responde, con el 11434 abierto) estás ante aislamiento de clientes.
Alternativas, en orden de preferencia:

1. **Tailscale** (o Zerotier), si lo tienen instalado o pueden
   instalarlo: crea una red privada entre sus equipos que atraviesa el
   aislamiento del wifi y el NAT, sin abrir nada a internet. Ambos
   equipos en la misma tailnet, y el cliente usa la IP `100.x.y.z` del
   servidor:

   ```ini
   OLLAMA_URL=http://<IP-TAILSCALE-DEL-SERVIDOR>:11434    # la 100.x.y.z, no la del wifi
   ```

   (Saca esa IP en el servidor con `tailscale ip -4`.)

   `OLLAMA_HOST=0.0.0.0` ya hace que Ollama escuche también en esa
   interfaz. En Windows, si el firewall bloquea, permite el 11434
   acotado al rango de Tailscale:

   ```powershell
   New-NetFirewallRule -DisplayName "Ollama Tailscale" -Direction Inbound `
     -Protocol TCP -LocalPort 11434 -Action Allow -RemoteAddress 100.64.0.0/10
   ```

   Sigue siendo una red privada entre *sus* dispositivos: nadie más de la
   tailnet ni de internet llega al puerto.
2. **Hotspot desde el móvil de alguien** (o desde el propio servidor) y
   que todos se conecten ahí. Es lo más rápido si no hay Tailscale.
3. Un router propio en modo AP, si tienen uno a mano.
4. Cable ethernet entre servidor y cliente (con un switch si son más).
5. Como último recurso, que cada quien corra su propio Ollama local
   (`OLLAMA_URL=http://localhost:11434`) con un modelo pequeño.

Vale la pena **probar esto en la primera hora del hackathon**, no
cuando falten diez minutos para la demo.

---

## 7. Nuestro caso: la red del Tec (HackMTY 2026)

Medido y **funcionando** el 2026-09-11, con el servidor en la laptop de
Aldo y el cliente en la de Diego:

| | Servidor | Cliente |
|---|---|---|
| IP | `10.22.233.98` | `10.22.168.56` |
| Máscara | `/20` (255.255.240.0) | `/20` |
| Subred | `10.22.224.0/20` | `10.22.160.0/20` |
| Gateway | `10.22.224.1` | `10.22.160.1` |

Son **subredes distintas con gateways distintos** — están en segmentos
separados del wifi del campus (`svcs.itesm.mx`) — y aun así la conexión
funciona: el router del Tec enruta TCP entre ellas. Lo que sí bloquea es
ICMP, de ahí que el ping se pierda al 100%.

Lo que hay que poner en el `.env` (ya viene en `.env.example`):

```ini
OLLAMA_URL=http://10.22.233.98:11434
OLLAMA_MODEL=qwen2.5:7b
```

Dos cosas que **van a** morder durante el evento:

- **Esa IP es DHCP.** En una red de campus cambia al reconectar el wifi,
  al cambiar de edificio o al reiniciar. Si de pronto deja de conectar,
  lo primero es pedirle al dueño del servidor su IP nueva (`ipconfig`) y
  correr `python -m scripts.check_ollama <IP-nueva>`.
- **El puerto está abierto a toda la red del campus**, que son miles de
  dispositivos, y Ollama no tiene autenticación: cualquiera que la
  encuentre puede usar el modelo, descargar otros o borrarlos. Para dos
  días de hackathon es un riesgo asumible, pero acota la regla de
  firewall a las IPs del equipo si puedes, y **borra la regla al
  terminar**:

  ```powershell
  Remove-NetFirewallRule -DisplayName "Ollama LAN"
  ```
