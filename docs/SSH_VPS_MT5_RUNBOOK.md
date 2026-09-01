# Runbook: acceso SSH de Dashboardv1 a VPS Windows con MT5

Esta es la forma validada para conectar Dashboardv1 a una VPS Windows que contiene terminales MT5. El Dashboard usa SFTP sobre SSH con una clave ED25519; no guarda contraseñas y puede funcionar solo como observador.

> Ejecute los pasos identificados como **VPS** dentro de la sesión RDP de la VPS, usando **Windows PowerShell como Administrador**. Los pasos identificados como **PC local** se ejecutan en el PC donde corre Dashboardv1.

## Principios y datos que hay que reunir

- El puerto RDP no es el puerto SSH. RDP suele usar `3389`; OpenSSH suele usar `22`. Confirme con el proveedor que el puerto SSH es accesible desde Internet o desde la red privada que vaya a usar.
- Necesite: host/IP, puerto SSH, usuario Windows (normalmente `Administrator`) y el `DataDir` de MT5. Un `DataDir` válido contiene `MQL5` y normalmente `origin.txt`.
- Cree una clave distinta para cada VPS. Nunca copie una clave privada a la VPS, al repositorio ni a un chat.
- Dashboardv1 fija la huella SHA256 de la clave de host. Si cambia la clave de host de la VPS, detenga la conexión y vuelva a verificarla antes de actualizar el Dashboard.

## 1. Preparar OpenSSH en la VPS

### VPS

Compruebe que el servidor existe, está iniciado y escucha en el puerto 22:

```powershell
Get-Service sshd
Set-Service sshd -StartupType Automatic
Start-Service sshd
netstat -ano | findstr :22
Enable-NetFirewallRule -Name OpenSSH-Server-In-TCP
```

El resultado esperado de `netstat` incluye `0.0.0.0:22` o `[::]:22` en estado `LISTENING`.

Si `sshd` no existe, instale **OpenSSH Server** como característica opcional de Windows o mediante el instalador oficial de Microsoft/OpenSSH; después repita estos comandos. Si desde fuera sigue sin ser accesible, el proveedor debe permitir o redirigir TCP/22 hacia esa VPS. Un puerto RDP publicado no permite SSH por sí mismo.

## 2. Crear una clave dedicada

### PC local

En PowerShell, cree la carpeta y genere una ED25519 sin passphrase. Para evitar que PowerShell convierta una cadena vacía en una passphrase literal, use el modo interactivo y pulse `Enter` dos veces cuando pregunte por la passphrase.

```powershell
$keyPath = 'C:\Users\Admin\.ssh\dashboard_nombre_vps_ed25519'
New-Item -ItemType Directory -Force -Path (Split-Path $keyPath) | Out-Null
ssh-keygen -t ed25519 -f $keyPath -C 'dashboard-vps-nombre'
Get-Content "$keyPath.pub"
```

Copie la única línea que empieza por `ssh-ed25519`. No copie el archivo privado sin extensión (`$keyPath`).

> Si la clave se creó accidentalmente con la passphrase literal `""`, corríjala en el PC local, sin cambiar la clave pública: `ssh-keygen -p -f $keyPath -P '""' -N ''`.

## 3. Autorizar únicamente la clave pública

### VPS

Para el usuario `Administrator` integrado en el grupo `Administrators`, OpenSSH usa normalmente este archivo global:

`C:\ProgramData\ssh\administrators_authorized_keys`

Pegue la línea pública en el marcador `$publicKey`. El comando conserva las claves existentes y evita añadir la misma clave dos veces.

```powershell
$authFile = 'C:\ProgramData\ssh\administrators_authorized_keys'
$publicKey = 'PEGA_AQUI_LA_LINEA_COMPLETA_ssh-ed25519'

New-Item -ItemType File -Force -Path $authFile | Out-Null
if (-not (Select-String -Path $authFile -SimpleMatch $publicKey -Quiet)) {
  Add-Content -Path $authFile -Value $publicKey
}
Get-Content $authFile
```

Fije los permisos requeridos. Deben quedar solo `SYSTEM` y `Administrators` con control total:

```powershell
icacls $authFile /inheritance:r
icacls $authFile /grant:r '*S-1-5-32-544:(F)' '*S-1-5-18:(F)'
icacls $authFile
```

No es necesario reiniciar `sshd` solo por añadir una clave: se lee al autenticar. Si cambió `sshd_config`, entonces sí use `Restart-Service sshd` desde la VPS, manteniendo la sesión RDP abierta como vía de recuperación.

Compruebe la configuración efectiva:

```powershell
Select-String -Path 'C:\ProgramData\ssh\sshd_config' -Pattern 'PubkeyAuthentication|AuthorizedKeysFile|Match Group administrators'
```

Debe existir un bloque equivalente a:

```text
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

El archivo debe llamarse exactamente `administrators_authorized_keys`, sin `.txt`. El Explorador puede ocultar extensiones; `Get-Item $authFile` es la comprobación fiable.

## 4. Probar la clave sin contraseña

### PC local

Sustituya host, puerto, usuario y ruta. `BatchMode` y las opciones de autenticación hacen que la prueba falle en lugar de pedir una contraseña; así se confirma que la clave es la que funciona.

```powershell
$hostName = 'IP_O_HOST_DE_LA_VPS'
$port = 22
$user = 'Administrator'
$keyPath = 'C:\Users\Admin\.ssh\dashboard_nombre_vps_ed25519'

ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -i $keyPath -p $port "$user@$hostName" whoami
```

El resultado esperado es algo como `NOMBRE-VPS\administrator`. Si pide contraseña o falla, no configure aún el Dashboard: revise el usuario, el puerto, el contenido de `administrators_authorized_keys`, sus permisos y el bloque `Match Group administrators`.

## 5. Obtener y verificar la huella del host SSH

### VPS

Obtenga la huella directamente en la VPS, por RDP:

```powershell
ssh-keygen -lf 'C:\ProgramData\ssh\ssh_host_ed25519_key.pub' -E sha256
```

### PC local

Como comprobación adicional, la huella expuesta por la red debe coincidir:

```powershell
ssh-keyscan -p $port -t ed25519 $hostName | ssh-keygen -lf - -E sha256
```

Registre el valor completo `SHA256:...` solo si ambos coinciden. `ssh-keyscan` por sí solo es una observación de red, no una prueba de identidad.

## 6. Registrar la terminal en Dashboardv1

En **Settings → Terminal registration**, use estos valores:

| Campo | Valor |
|---|---|
| Transport | `ssh` |
| Host | IP o DNS de la VPS |
| Port | puerto SSH, normalmente `22` |
| Username | usuario SSH/Windows, normalmente `Administrator` |
| SSH private key | ruta local de la clave ED25519, por ejemplo `C:\Users\Admin\.ssh\dashboard_nombre_vps_ed25519` |
| SSH host fingerprint | valor validado `SHA256:...` |
| DataDir | ruta Windows remota completa del DataDir MT5 |

Dashboardv1 exige todos esos campos para una terminal SSH. El conector verifica la huella antes de abrir SFTP. No se registra ni se requiere la contraseña Windows/RDP.

## 7. Caso especial: GuardianCuenta solo observador

Para el Guardián, el Dashboard debe leer únicamente:

```text
<DataDir>\MQL5\Files\GuardianCuenta\GuardianCuenta_v110_events.csv
<DataDir>\MQL5\Files\GuardianCuenta\GuardianCuenta_v110_position_state.csv
<DataDir>\MQL5\Files\GuardianCuenta\GuardianCuenta_v110_virtual_state.csv
```

Después de registrar la terminal SSH, regístrela como fuente Guardian en el backend. Esta configuración excluye automáticamente la terminal del ciclo `DashboardBridge`: el Dashboard no crea solicitudes, no altera MT5 y no realiza operaciones.

Obtenga primero el ID local de la terminal registrada:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/terminals' | Select-Object id,name,remote_data_dir
```

Después sustituya `ID_DE_LA_TERMINAL` por el valor correcto:

```powershell
..\.venv\Scripts\python.exe -c "from app.guardian_monitor import register_source; print(register_source(ID_DE_LA_TERMINAL))"
```

Confirme desde `Guardian → Actualizar ahora` que la fuente queda `connected` y que los tres CSV se leen. Cualquier error se conserva como `last_error` localmente en Dashboardv1; no se escriben archivos en la VPS.

## Diagnóstico rápido

| Síntoma | Comprobación / solución |
|---|---|
| `Connection timed out` | El proveedor o firewall no expone TCP/22; confirme IP, puerto y regla de red. |
| `Connection refused` | `sshd` no escucha en ese puerto o el firewall local lo bloquea. |
| `Invalid user Administrator` | Confirme el usuario real de Windows/SSH en la VPS y que OpenSSH está configurado para él. |
| `Permission denied (publickey)` | La clave pública no coincide, está en el archivo equivocado, tiene extensión `.txt` o ACL incorrecta. |
| Huella no coincide | No continúe: verifique la huella del host por RDP/proveedor; puede haber una reinstalación o un host distinto. |
| Dashboard no carga la clave | Compruebe que la ruta privada existe en el PC local y que no tiene passphrase si el conector no la admite. |

## Reglas de seguridad permanentes

- Mantenga RDP abierto mientras prueba cambios de SSH.
- Una clave por VPS; revóquela eliminando solo su línea pública del archivo de claves de la VPS.
- No envíe claves privadas, contraseñas ni el contenido completo de `authorized_keys` por correo/chat.
- Para una VPS que deba ser solo observada, use las lecturas SFTP y no instale ni active `DashboardBridge`.
