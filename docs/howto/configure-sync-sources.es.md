---
source_sha: "80123e3974ff"
---

# Configura las fuentes de sincronización { #configure-sync-sources }

Las fuentes de sincronización traen documentos de servicios externos (Google
Drive, S3/MinIO, un sitio web público, un repositorio Git, un sitio de SharePoint,
un OneDrive o una carpeta de un servidor SFTP) a las colecciones de conocimiento por su cuenta. Cada fuente guarda un tipo de connector, una colección de destino,
opciones propias del connector, un modo de sincronización, un horario opcional y
el id del [secreto del vault](../secrets.md) que la autentica - un sitio web no
necesita ninguno.

Cuando se ejecuta una sincronización, el connector lista los archivos remotos,
los descarga a un directorio temporal y los pasa por la cadena de ingesta
habitual (parsear, trocear, embeber, almacenar). Cuando el listado está completo,
se eliminan los documentos que la fuente trajo antes y ya no lista. Una entrada
de `SyncLog` deja constancia del resultado de cada operación de sincronización.

### La arquitectura de un vistazo { #architecture-at-a-glance }

| Componente | Ubicación | Función |
|-----------|----------|------|
| `BaseSyncConnector` | `app/services/rag/connectors/__init__.py` | Base abstracta de todos los connectores |
| `RemoteFile` | `app/services/rag/connectors/__init__.py` | Modelo de Pydantic que describe un archivo remoto |
| `CONNECTOR_REGISTRY` | `app/services/rag/connectors/__init__.py` | Asocia las cadenas de tipo de connector con sus clases |
| `SyncSource` (modelo de BD) | `app/db/models/sync_source.py` | Persiste la configuración de las fuentes |
| `SyncLog` (modelo de BD) | `app/db/models/sync_log.py` | Registra cada operación de sincronización |
| `SyncSourceService` | `app/services/sync_source.py` | Lógica de negocio del CRUD y del disparo manual |
| Comandos CLI de RAG | `app/commands/rag.py` | Interfaz de línea de comandos para gestionar fuentes |
| Rutas de la API de RAG | `app/api/routes/v1/rag.py` | API REST para gestionar fuentes |

## Inicio rápido -- CLI { #quick-start-cli }

### Listar los tipos de connector disponibles { #list-available-connector-types }

```bash
# Lists the configured sources and the connector type of each
uv run agenticos cmd rag-sources
```

Para ver qué tipos de connector tiene este deployment (`gdrive`, `s3`, `web`, `git`,
`sharepoint`, `sftp`), usa `GET /api/v1/rag/sync/connectors`, como en [Ejemplo:
descubrir los connectores disponibles](#example-discover-available-connectors).

### Añadir una fuente de Google Drive -- sincronización cada 2 horas { #add-a-google-drive-source-sync-every-2-hours }

```bash
uv run agenticos cmd rag-source-add \
  --name "Legal docs" \
  --type gdrive \
  --org 0c8f2b1e-... \
  --collection legal \
  --config '{"folder_id": "1abc123def", "include_subfolders": true}' \
  --sync-mode new_only \
  --schedule 120
```

### Añadir una fuente de S3 -- solo sincronización manual { #add-an-s3-source-manual-sync-only }

```bash
uv run agenticos cmd rag-source-add \
  --name "Marketing" \
  --type s3 \
  --org 0c8f2b1e-... \
  --collection marketing \
  --config '{"bucket": "my-docs", "prefix": "marketing/"}' \
  --sync-mode full \
  --schedule 0
```

### Añadir una fuente de Git -- la documentación de un repositorio, cada noche { #add-a-git-source-a-repositorys-docs-nightly }

```bash
uv run agenticos cmd rag-source-add \
  --name "Handbook" \
  --type git \
  --org 0c8f2b1e-... \
  --collection handbook \
  --config '{"repository_url": "https://github.com/acme/handbook.git", "branch": "main", "path_prefix": "docs"}' \
  --sync-mode new_only \
  --schedule 1440
```

Después elige su token de acceso como credencial de la fuente en la interfaz, o
envía `secret_id` con un `PATCH` — consulta
[Configurar un repositorio Git](#git-repository-setup).

### Añadir una fuente de SharePoint -- una carpeta de una biblioteca, cada 6 horas { #add-a-sharepoint-source-one-library-folder-every-6-hours }

```bash
uv run agenticos cmd rag-source-add \
  --name "HR policies" \
  --type sharepoint \
  --org 0c8f2b1e-... \
  --collection hr \
  --secret-id <vault-secret-id> \
  --config '{"site_url": "https://contoso.sharepoint.com/sites/HR", "folder_path": "Policies"}' \
  --sync-mode new_only \
  --schedule 360
```

`--secret-id` es la app de Microsoft Entra en el vault de la organización —
consulta [Configurar SharePoint y OneDrive](#sharepoint-and-onedrive-setup).

### Añadir una fuente de SFTP -- una carpeta, cada noche { #add-an-sftp-source-one-folder-nightly }

```bash
uv run agenticos cmd rag-source-add \
  --name "Partner reports" \
  --type sftp \
  --org 0c8f2b1e-... \
  --collection partner-reports \
  --secret-id <vault-secret-id> \
  --config '{"host": "sftp.example.com", "root_path": "/outgoing", "host_key": "ssh-ed25519 AAAAC3Nz..."}' \
  --sync-mode new_only \
  --schedule 1440
```

`--secret-id` es el login SSH en el vault de la organización, y `host_key` es la
clave pública de host del servidor — consulta [Configurar SFTP](#sftp-setup).

### Lanzar una sincronización a mano { #trigger-sync-manually }

```bash
# Sync a single source by ID
uv run agenticos cmd rag-source-sync <source-id>

# Sync all active sources
uv run agenticos cmd rag-source-sync --all
```

### Eliminar una fuente { #remove-a-source }

```bash
uv run agenticos cmd rag-source-remove <source-id>
```

El `<source-id>` es un UUID que se imprime al crear la fuente y que aparece en
el listado de `rag-sources`.

## Inicio rápido -- interfaz { #quick-start-ui }

1. Ve a **Knowledge Base** y abre la pestaña **Sync**.
2. Pulsa **"+ Add Source"**.
3. Elige un tipo de connector (Google Drive, S3, Website, Git
   repository, SharePoint & OneDrive, SFTP server). Los campos del
   formulario se generan a partir del JSON Schema del `CONFIG_MODEL` del
   connector. Un sitio web no tiene paso de credencial.
4. Rellena los campos de configuración propios del connector (por ejemplo, el
   ID de la carpeta o el nombre del bucket).
5. Elige una colección de destino, un modo de sincronización y un intervalo de
   ejecución.
6. Pulsa **"Create Source"**.
7. Usa el botón **"Sync Now"** para lanzar una sincronización inmediata, o
   espera a que el horario la dispare por sí solo.

Para cambiar una fuente más adelante, pulsa el lápiz de su fila. Se abre el mismo
asistente con los valores de la fuente ya rellenados. El connector no se puede
cambiar, y **"Save Changes"** envía solo los campos que hayas cambiado. La fuente
sigue en la base de conocimiento en la que está.

La interfaz llama a la misma API REST que se documenta más abajo, así que todo
lo que puedes hacer desde ella lo puedes hacer también con `curl` o con
cualquier cliente HTTP.

## Modos de sincronización { #sync-modes }

| Modo | Comportamiento |
|------|----------|
| `full` | Sincroniza todo de nuevo. Todos los archivos se (re)ingestan y los documentos existentes se reemplazan. Ignora las revisiones y los hashes, así que ejecútalo cuando la revisión de un connector pueda pasar por alto un cambio. |
| `new_only` | Añade los archivos nuevos y actualiza los que han cambiado. Usa la revisión del connector y después un hash SHA-256 para detectar cambios: los archivos sin cambios se omiten. |
| `update_only` | Solo actualiza archivos que ya están en la colección. Los archivos nuevos se omiten. Usa la revisión del connector y después un hash SHA-256 para omitir los archivos sin cambios. |

!!! tip "`new_only` para casi todos los flujos"

    Añade los archivos nuevos y actualiza los modificados mientras omite los que
    no han cambiado, que es la sincronización incremental más rápida.
    `update_only` refresca los documentos existentes sin añadir ninguno nuevo;
    `full` es una reimportación limpia cada vez.

### Qué elimina una sincronización { #what-a-sync-removes }

En todos los modos, una sincronización elimina los documentos que su fuente trajo
antes y ya no lista: una página retirada del sitio, un archivo borrado de la
carpeta de Drive, un objeto quitado del bucket. El registro de sincronización los
cuenta en `removed`.

No elimina nada salvo que el listado estuviera **completo**. Un crawl que se
detuvo en su límite de páginas, o que no pudo leer una de ellas, no ha visto lo
que no lista. Ese run conserva todos los documentos y lo indica en el mensaje del
registro de sincronización. La siguiente sincronización con un listado completo
elimina lo que ya no está. Un documento que no se pudo eliminar cuenta como un
archivo fallido, y la siguiente sincronización lo vuelve a intentar.

Cada fuente ejecuta una sola sincronización a la vez. Una sincronización iniciada
mientras otra de la misma fuente sigue en marcha no arranca, y su registro lo
indica.

Solo se eliminan los documentos de la propia fuente. Una subida, o un documento
que otra fuente trajo a la misma colección, no se toca nunca. Cuando dos
fuentes de una colección listan el mismo documento, este se conserva hasta que
ambas dejan de listarlo. Un documento ingestado antes de que su fuente
registrara esto (septiembre de 2026) se conserva hasta que una sincronización de
la fuente vuelva a listarlo.

### Qué hace una segunda sincronización { #what-a-second-sync-does }

Una sincronización posterior a la primera hace tan poco como la fuente le
permite:

- **Un archivo sin cambios cuesta como mucho una descarga, nunca un embedding.**
  Su SHA-256 coincide con el del documento almacenado, así que se cuenta como
  `skipped` y no se vuelve a parsear ni a embeber. Un connector que informa de una
  revisión para cada archivo se ahorra también la descarga. S3 informa del `ETag`
  de cada objeto, así que `new_only` y `update_only` omiten un objeto con el
  `ETag` sin cambios antes de la descarga.

  Un servidor SFTP informa del tamaño y la hora de modificación de cada archivo,
  así que los mismos modos omiten un archivo cuyo tamaño y hora no han cambiado.
  Esa revisión pasa por alto una edición que conserva los dos. `full` descarga
  todos los archivos, y lee una edición así.

- **Una fuente sin cambios cuesta una petición.** Un connector capaz de decir en
  qué punto está todo su contenido —el commit de cabeza de una rama de Git, el
  feed de cambios de una biblioteca de SharePoint— lo registra después de cada ejecución que termina sin ningún fallo. La siguiente
  ejecución `new_only` o `update_only` que encuentra el mismo valor, con la misma
  configuración, se detiene antes de listar nada: su registro no muestra ningún
  archivo procesado, y no elimina nada. Cambiar la configuración, la colección o el modo hace que la
  siguiente ejecución lo vuelva a leer todo, y `full` nunca se detiene antes.
- **Un archivo que una sincronización interrumpida dejó a medias se corrige.**
  Un worker detenido después de guardar los vectores de un archivo y antes de
  registrarlos los deja sin ninguna fila que los siga. La siguiente sincronización
  de esa fuente borra lo que nada sigue y vuelve a ingerir el archivo; mientras
  un archivo así espera, no se detiene antes.

Una ejecución con un archivo fallido no registra ningún estado, así que la
siguiente lee la fuente entera y vuelve a intentarlo.

## Horario { #schedule }

El campo `schedule_minutes` controla con qué frecuencia se sincroniza la fuente
por sí sola:

| Valor | Significado |
|-------|---------|
| `0` (o `null`) | Solo manual -- se lanza desde la CLI o la interfaz |
| `30` | Cada 30 minutos |
| `120` | Cada 2 horas |
| `1440` | Una vez al día |

!!! warning "Un horario necesita el worker"

    La programación `dispatch-due-syncs` despierta cada 60 segundos y encola una
    sincronización por cada fuente que toque, así que `schedule_minutes` no hace
    nada sin el contenedor `worker` que arranca `make dev`. Sin él, un disparo
    manual (CLI, API o la interfaz) también encola su sincronización, que
    igualmente espera a un worker. Una fuente nunca tiene dos sincronizaciones a
    la vez.

## Configurar Google Drive { #google-drive-setup }

### 1. Crea una cuenta de servicio { #1-create-a-service-account }

1. Entra en la [Google Cloud Console](https://console.cloud.google.com/).
2. Crea un proyecto nuevo (o selecciona uno existente).
3. Activa la **Google Drive API**.
4. Ve a **IAM & Admin > Service Accounts** y crea una cuenta de servicio nueva.
5. Crea una clave JSON para la cuenta de servicio y descárgala.

### 2. Comparte tu carpeta de Drive { #2-share-your-drive-folder }

1. Abre Google Drive y ve a la carpeta que quieres sincronizar.
2. Pulsa **Share** y añade la dirección de correo de la cuenta de servicio
   (tiene la forma `name@project.iam.gserviceaccount.com`).
3. Concédele al menos acceso **Viewer**.

### 3. Dale la clave a la fuente { #3-give-the-source-the-key }

Pega el contenido del archivo de clave JSON en el campo **Service Account JSON**
de la fuente. Una fuente `gdrive` funciona con la credencial que lleva su propia
configuración y con nada más: no hay un valor de respaldo para todo el
despliegue, porque uno así dejaría que el `folder_id` de una fuente decidiera qué
se lista bajo la cuenta de servicio del operador.

`GOOGLE_DRIVE_CREDENTIALS_FILE` en `.env` sirve únicamente para el comando de
CLI `rag-sync-gdrive`.

### 4. Averigua el ID de la carpeta { #4-get-the-folder-id }

El ID de la carpeta es el último segmento de la URL de la carpeta de Google
Drive:

```
https://drive.google.com/drive/folders/1abc123def456ghi
                                        ^^^^^^^^^^^^^^^
                                        This is the folder ID
```

### 5. Campos de configuración del connector de Google Drive { #5-google-drive-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `folder_id` | string | Sí | -- | El ID de la carpeta de Google Drive, tomado de la URL |
| `include_subfolders` | boolean | No | `true` | Incluir de forma recursiva los archivos de las subcarpetas |

La cuenta de servicio en sí **no** es un campo de configuración. Guárdala en el
vault como credencial de tipo `gcp_service_account` y apunta la fuente hacia ella
con `secret_id`: así se almacena una sola vez y la referencia cada fuente que la
necesite, en lugar de pegarla en cada una
([#937](https://github.com/vstorm-co/agenticos/issues/937)). Enviarla bajo
`config` se rechaza.

Un `folder_id` solo puede contener lo que Google emite: letras, dígitos, `-` y
`_`. Cualquier otra cosa se rechaza al crear la fuente, porque el id se
interpola en la consulta de Drive y una comilla simple dentro de él amplía lo
que esa consulta lista.

Los archivos de Google Docs, Sheets y Slides se exportan por sí solos a formatos
portables (PDF, XLSX, PPTX) durante la descarga. Un archivo cuyo nombre en Drive
contiene separadores de ruta se escribe como un único archivo dentro del
directorio de sincronización, nunca en la ruta que deletrea su nombre.

## Configurar S3 / MinIO { #s3-minio-setup }

### 1. Configura el entorno { #1-configure-the-environment }

Añade estas variables a tu `.env`:

```bash
S3_RAG_ENDPOINT=https://s3.amazonaws.com   # or your MinIO URL, e.g. http://localhost:9000
S3_RAG_ACCESS_KEY=your-access-key
S3_RAG_SECRET_KEY=your-secret-key
S3_RAG_REGION=us-east-1                    # required for AWS, optional for MinIO
```

En MinIO, el endpoint suele ser `http://minio:9000` (Docker) o
`http://localhost:9000` (local).

### 2. Campos de configuración del connector de S3 { #2-s3-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `bucket` | string | Sí | -- | Nombre del bucket de S3 |
| `prefix` | string | No | `""` | Prefijo de clave que acota el alcance de la sincronización (por ejemplo, `documents/legal/`). Déjalo vacío para el bucket entero. |

## Configurar un sitio web { #website-setup }

Una fuente `web` lee un sitio web público, normalmente el sitio de documentación
de un producto. No necesita credencial ni entrada en el vault. Dale una URL de
inicio y, o bien sigue los enlaces desde esa página, o bien lee las páginas que
lista un sitemap.

```bash
uv run agenticos cmd rag-source-add \
  --name "Product docs" \
  --type web \
  --org 0c8f2b1e-... \
  --collection product-docs \
  --config '{"root_url": "https://docs.example.com/guide/", "max_depth": 3}' \
  --sync-mode new_only \
  --schedule 1440
```

### Campos de configuración del connector de sitio web { #website-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `root_url` | string | Sí | -- | La página desde la que empieza el crawl. Su host es el único host que lee la fuente. |
| `max_depth` | integer | No | `2` | A cuántos enlaces de distancia de la URL de inicio seguir, de `0` a `10`. `0` lee solo la página de inicio. |
| `path_prefix` | string | No | la carpeta de la URL de inicio | Solo se leen las páginas cuya ruta empieza por esto. `https://docs.example.com/guide/intro` lee `/guide/` por defecto; pon `/` para el host entero. |
| `sitemap_url` | string | No | -- | Lee las páginas que lista este sitemap en lugar de seguir enlaces. Tiene que estar en el host de la URL de inicio y usar `https://` cuando la URL de inicio lo usa. Un índice de sitemaps se sigue hasta sus sitemaps. |
| `max_pages` | integer | No | `500` | El crawl se detiene tras leer este número de páginas, de `1` a `5000`. |

### Qué acota un crawl { #what-bounds-a-crawl }

- **Un host y una ruta.** Los enlaces a otros hosts, y a rutas fuera de
  `path_prefix`, no se siguen. Una redirección que salga de ellos tampoco se
  sigue. Una URL de inicio en `https://` nunca se abandona por `http://`: no se
  sigue un enlace ni una redirección a una página sin cifrar.
- **La red del deployment queda fuera de alcance.** Cada petición - robots.txt,
  el sitemap, cada página y cada redirección - se comprueba contra la misma
  política SSRF que los webhooks y los servidores MCP. Se envía a la dirección que
  pasó la comprobación. Una URL de inicio que resuelve a una dirección privada, de
  loopback, link-local o de metadatos de la nube se rechaza al guardar la fuente.
- **Se respeta robots.txt** para sitemaps y páginas, incluido `Crawl-delay` de
  hasta diez segundos. El crawler se identifica como `AgenticOS-Crawler`. Espera
  al menos medio segundo entre peticiones, y se respeta una página que diga
  `noindex` o `nofollow`. Una página que un sitemap sigue listando después de
  decir `noindex`, o de desaparecer, se elimina de la colección.
- **Tamaño y tiempo.** Una página de más de 5 MB no se lee. El crawl se detiene en
  `max_pages`. Una sincronización deja de leer el sitio al cabo de seis horas, y
  una sincronización que se detuvo no elimina nada.

Cada página se guarda como un documento Markdown que contiene su texto y la URL
de la que procede, sin su query string. La navegación, las cabeceras, los pies de página y los scripts
se dejan fuera. Una página solo se vuelve a embeber cuando cambia su texto. Un
nuevo sello de build o un script de seguimiento en el marcado no cuentan como
cambio.

### Quién puede leer lo que importa { #who-can-read-what-it-imports }

Una fuente de sitio web no tiene credencial, así que su alcance es lo que el
sitio muestra a cualquiera en internet. Nunca pasa de un login. Todo lo que
importa lo puede buscar cualquiera que pueda buscar en la colección que alimenta,
como con cualquier otra fuente. Consulta
[quién acaba pudiendo leer lo que ingirió una fuente](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Solo se importan páginas HTML. Un PDF u otro archivo enlazado desde una página no
se descarga.

## Configurar un repositorio Git { #git-repository-setup }

Una fuente `git` lee la documentación de un repositorio por HTTPS: GitHub, GitLab
o cualquier otro host que sirva git por HTTPS. Necesita la URL de clonado y un
token de acceso, no la API de ninguna de las dos plataformas.

### 1. Emite un token para ese único repositorio { #1-issue-a-token-for-the-one-repository }

**El alcance del token es el alcance de la fuente.** Todo lo que la fuente
ingiere pasa a poder buscarlo cualquiera que pueda leer la colección, así que un
token capaz de leer todos los repositorios privados que puede leer su dueño es un
token capaz de publicarlos todos para ese público. Consulta [quién acaba pudiendo
leer lo que ingirió una
fuente](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

- **GitHub:** un personal access token de tipo fine-grained, con *Only select
  repositories*, ese único repositorio y **Contents: Read-only** como único
  permiso.
- **GitLab:** un project access token en ese único proyecto, con el rol
  **Reporter** y solo el scope **`read_repository`**.

Ponle una fecha de caducidad. Cuando caduque, la siguiente sincronización de la
fuente falla con *the repository refused the source's token*, y la solución es un
token nuevo en el mismo secreto del vault.

### 2. Añádelo al vault { #2-add-it-to-the-vault }

Añade el token al vault como **Git access token**, con el **host** al que
pertenece: `github.com`, `gitlab.com` o tu propio servidor, como
`git.example.com:8443`. Un host con letras que no son ASCII se escribe en su forma
codificada, por ejemplo `xn--bcher-kva.example` para `bücher.example`. Después
elige el token en el paso de credencial de la fuente. Se
envía en una cabecera HTTP `Authorization`, nunca en la URL ni en una línea de
comandos que otro proceso pueda leer.

**El host es del token, no de la fuente.** Quien edita una fuente elige la URL de
su repositorio, y un token solo se envía al host con el que se añadió. Así que
editar una fuente no puede dirigir el token de la organización a otro servidor, y
ningún otro tipo de clave, como la API key de un provider de modelos, puede
elegirse para una fuente de Git.

### 3. Campos de configuración del connector de Git { #3-git-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `repository_url` | string | Sí | -- | La URL de clonado HTTPS, por ejemplo `https://github.com/acme/handbook.git`. Sin nombre de usuario ni token. |
| `branch` | string | No | `main` | La rama que se lee. |
| `path_prefix` | string | No | -- | Un directorio dentro del repositorio, por ejemplo `docs`. Déjalo vacío para el repositorio entero. |
| `include` | lista de strings | No | `**/*.md`, `**/*.txt` | Qué archivos se ingieren, como patrones al estilo de `.gitignore` relativos a `path_prefix`. |

El valor por defecto es la documentación, no el árbol entero: el código fuente de
un repositorio no es un corpus, e ingerirlo llena la base de conocimiento de
código que nadie ha pedido buscar. Añade un patrón como `**/*.pdf` para otro
formato que lea el parser de la colección. Un patrón no puede empezar por `!`.

Cada archivo es un documento cuya dirección es
`git://<host>/<owner>/<repo>@<branch>/<path>`, con `:<port>` tras el host cuando
el puerto no es 443. La rama forma parte de la dirección, así que dos fuentes que leen dos ramas de un mismo repositorio en una
misma colección mantienen documentos separados.

### 4. Qué transfiere una sincronización { #4-what-a-sync-transfers }

La primera petición de cada sincronización es `git ls-remote` para la rama: más o
menos un kilobyte. Si el commit de cabeza no se ha movido desde la última
ejecución limpia, la sincronización se detiene ahí. Si se ha movido, el connector
hace un clonado superficial, parcial y disperso (shallow, partial, sparse): un
solo commit, y solo los archivos que casan con los patrones de inclusión. Por eso
la documentación de un monorepo cuesta lo que su documentación, no lo que su
árbol de código.

Antes de que el clonado escriba nada en el disco del worker, el connector mide
cada archivo que escribiría. Un archivo que supera el límite de documento de la
base de conocimiento (`MAX_UPLOAD_SIZE_MB`, 50 MB por defecto), o más de 512 MB
de archivos en total, se rechaza, y no se escribe nada.

Los enlaces simbólicos y los submódulos no se siguen, y un enlace no se ingiere
como documento.

### Reglas de red { #network-rules }

La URL tiene que ser `https://`. Su host se resuelve una sola vez y se comprueba
como cualquier otra dirección que elige un inquilino: un host que resuelve a una
dirección privada, de loopback o link-local se rechaza al guardar la fuente y de
nuevo al sincronizar, y git solo se conecta a las direcciones que esa
comprobación aprobó. Las redirecciones no se siguen. Un deployment detrás de un
proxy de salida (`HTTPS_PROXY`) lo sigue usando; en ese caso el proxy resuelve el
host por su cuenta.

La imagen del worker incluye `git`. Un worker construido a partir de otra imagen
necesita `git` 2.37 o posterior en su `PATH`.

## Configurar SharePoint y OneDrive { #sharepoint-and-onedrive-setup }

Una fuente `sharepoint` lee una biblioteca de documentos a través de Microsoft
Graph: la biblioteca de un sitio de SharePoint, o el OneDrive de una persona, que
Microsoft 365 guarda como un sitio propio. Puede leer la biblioteca entera o una
carpeta dentro de ella. Inicia sesión como un registro de aplicación de Microsoft
Entra que un administrador de tu tenant crea una sola vez.

### 1. Registra una app en Microsoft Entra { #1-register-an-app-in-microsoft-entra }

En el [Microsoft Entra admin center](https://entra.microsoft.com), abre
**App registrations → New registration**. Dale un nombre como *AgenticOS
sync*, deja **Accounts in this organizational directory only** y deja vacío el
redirect URI. La fuente inicia sesión como la propia app, no como una persona.

En la **Overview** de la app, anota el **Application (client) ID** y el
**Directory (tenant) ID**. En **Certificates & secrets**, añade un client secret
y copia su **Value**. El valor se muestra una sola vez. El *ID* del secreto no es
lo que necesita la fuente.

### 2. Concédele un sitio, no el tenant { #2-grant-it-one-site-not-the-tenant }

**El alcance de la app es el alcance de la fuente.** Todo lo que la fuente
ingiere pasa a poder buscarlo cualquiera que pueda leer la colección que
alimenta. Consulta [quién acaba pudiendo leer lo que ingirió una
fuente](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

En **API permissions**, añade el permiso de **aplicación** de Microsoft Graph
**`Sites.Selected`** y concede el consentimiento de administrador para él. Por sí
solo, `Sites.Selected` no lee nada. Después, un administrador concede a la app
acceso de lectura al único sitio que lee la fuente:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{
  "roles": ["read"],
  "grantedToIdentities": [
    {"application": {"id": "<client-id>", "displayName": "AgenticOS sync"}}
  ]
}
```

Esa llamada necesita `Sites.FullControl.All`, así que un administrador la hace
desde Graph Explorer o con `Grant-PnPAzureADAppSitePermission` de PnP PowerShell,
y no con la propia app.
`GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/HR?$select=id`
devuelve el id del sitio. Un OneDrive se concede de la misma forma, a través de su
sitio: `contoso-my.sharepoint.com:/personal/jane_contoso_com`.

!!! danger "`Files.Read.All` o `Sites.Read.All` hacen legibles todas las bibliotecas del tenant"

    Esos permisos se consienten para todo el tenant. Una fuente que los tiene
    sigue leyendo solo la biblioteca que nombra. Pero quien puede editar la
    fuente puede apuntar su URL de sitio a cualquier sitio u OneDrive de la
    organización, y la siguiente sincronización hace que esa biblioteca pueda
    buscarla cualquiera que pueda leer la colección. Nada en este producto puede
    saber qué permiso se dio a la app, porque un token no lo dice. Usa
    `Sites.Selected` y concede una app por cada público.

### 3. Añádela al vault { #3-add-it-to-the-vault }

Añade la app al vault como **Microsoft Entra app**: el id del tenant (o el
dominio del tenant, como `contoso.onmicrosoft.com`), el client id y el valor del
client secret. Después elígela en el paso de credencial de la fuente. El secreto
solo se envía a `login.microsoftonline.com`, para iniciar sesión.

Un client secret caduca, como mucho a los dos años. Cuando ha caducado, la
siguiente sincronización falla con *Microsoft Entra refused the app
registration's credentials (invalid_client)*. Añade un secreto nuevo a la app y
sustituye el valor en el mismo secreto del vault. Todas las fuentes que lo usan
recogen el valor nuevo en su siguiente sincronización.

### 4. Campos de configuración del connector de SharePoint { #4-sharepoint-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `site_url` | string | Sí | -- | El sitio, por ejemplo `https://contoso.sharepoint.com/sites/HR`, o un OneDrive, por ejemplo `https://contoso-my.sharepoint.com/personal/jane_contoso_com`. Solo el sitio: una URL de biblioteca o de página copiada del navegador se rechaza. |
| `library` | string | No | la biblioteca por defecto del sitio | El nombre de la biblioteca tal como lo muestra SharePoint, por ejemplo `Documents`. Un OneDrive solo tiene su biblioteca por defecto. |
| `folder_path` | string | No | -- | Una carpeta dentro de la biblioteca, por ejemplo `Policies/HR`. Déjalo vacío para la biblioteca entera. |
| `include_subfolders` | boolean | No | `true` | Si se leen las carpetas que hay bajo `folder_path`. |
| `extensions` | lista de strings | No | `.pdf`, `.docx`, `.md`, `.txt` | Qué tipos de archivo se ingieren, como mucho 100, cada uno de como mucho 16 caracteres. Añade `.pptx`, `.xlsx` u otro tipo solo si el parser de la colección lo lee: un archivo que el parser no puede leer cuenta como archivo fallido en cada sincronización. |

Cada archivo es un documento cuya dirección es `sharepoint://<drive-id>/<item-id>`.
La dirección sigue al archivo, no a su ruta, así que un archivo que se renombra o
se mueve dentro de la biblioteca conserva su documento. El documento conserva el
nombre de archivo con el que se ingirió hasta que cambia el contenido del archivo.

Los blocs de notas de OneNote y los archivos de otros tipos no se ingieren.

### 5. Qué transfiere una sincronización { #5-what-a-sync-transfers }

Cada sincronización empieza por el feed de cambios de Microsoft Graph para la
biblioteca. Cuando nada en la biblioteca ha cambiado desde la última ejecución
limpia, la sincronización se detiene ahí: una petición y ningún listado. El feed
cubre la biblioteca entera, porque Graph solo lo ofrece para la raíz de una
biblioteca. Por eso un cambio en otra carpeta de la misma biblioteca hace que la
siguiente sincronización vuelva a listar la carpeta de la fuente.

Cuando algo ha cambiado, la sincronización lista la carpeta y descarga todos los
archivos de los tipos configurados. Un archivo cuyo contenido no ha cambiado se
omite entonces antes de embeberlo, así que solo los archivos nuevos y modificados
cuestan un embedding. Un archivo se descarga desde la dirección que Graph da para
él, sin el token de la app. Un archivo que supera el límite de documento de la
base de conocimiento (`MAX_UPLOAD_SIZE_MB`, 50 MB por defecto) no se descarga y
cuenta como archivo fallido.

Graph frena a un cliente que lee deprisa. Una petición que Graph limita (HTTP
429) o que falla con un 5xx o un error de red se intenta cuatro veces en total, y
la sincronización espera lo que pida el `Retry-After` de Graph, hasta un minuto.
Una carpeta que aun así no se puede listar se nombra en el registro de
sincronización, y esa sincronización no elimina nada, porque no se vieron los
archivos de esa carpeta. Los archivos de las demás carpetas se ingieren igualmente.

Un archivo borrado después del listado y antes de su descarga se elimina como un
archivo que el listado no nombró.

### Red { #network }

El worker necesita HTTPS de salida hacia `login.microsoftonline.com`,
`graph.microsoft.com` y los hosts `*.sharepoint.com` de tu tenant. Nada de lo que
escribe quien edita una fuente se convierte en una dirección a la que se conecte
el worker: la URL del sitio es solo un nombre que Graph busca. Las nubes
nacionales de Microsoft, como US Government y China, usan otros hosts y no están
soportadas.

## Configurar SFTP { #sftp-setup }

Una fuente `sftp` lee un árbol de carpetas de un servidor SFTP sobre SSH, como la
carpeta de entrega de un socio o un servidor de archivos. Inicia sesión con un
login SSH del vault. Solo confía en la clave de host de su propia configuración, y
no lee nada de la configuración SSH propia del worker.

### 1. Obtén la clave de host del servidor { #1-get-the-servers-host-key }

La clave de host es la única prueba de que el worker habla con el servidor que
nombra la fuente. Pídesela al administrador del servidor. O léela tú mismo del
servidor y compara su huella con la del administrador:

```bash
ssh-keyscan -t ed25519 -p 22 sftp.example.com > host_key.txt
ssh-keygen -lf host_key.txt
```

`ssh-keyscan` lee la clave a través de la red, así que no puede demostrar que la
clave sea la del servidor. `ssh-keygen -lf` imprime la huella `SHA256:` de la
clave. El administrador lee la misma huella en el servidor con
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. Si las dos no son iguales, no
uses la clave.

`ssh-keyscan` escribe el nombre del host delante de la clave. La fuente solo
necesita el tipo de clave y la clave:

```bash
cut -d' ' -f2- host_key.txt
```

El connector fija una clave de tipo `ssh-ed25519`, `ssh-ed448`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521` o `ssh-rsa`.
Para un servidor sin clave Ed25519, usa `-t ecdsa` o `-t rsa`. El worker solo pide
al servidor una clave del tipo fijado. Un servidor con claves de varios tipos
responde con la clave de ese tipo.

Un servidor sin ninguna clave del tipo fijado termina la primera sincronización
mientras se abre la conexión. La frase del registro de sincronización depende del
servidor: [se perdió la conexión](#sftp-the-connection-to-was-lost), [el servidor
cerró la conexión](#sftp-closed-the-connection), o [los dos no pudieron ponerse de
acuerdo](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher). En
cada uno de los tres casos, comprueba primero el tipo de clave.

### 2. Añade un login SSH al vault { #2-add-an-ssh-login-to-the-vault }

**El alcance de la cuenta es el alcance de la fuente.** Todo lo que la fuente
ingiere pasa a poder buscarlo cualquiera que pueda leer la colección que
alimenta. Pide al administrador una cuenta que solo pueda leer la carpeta que lee
la fuente. Consulta [quién acaba pudiendo leer lo que ingirió una
fuente](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Pide también al administrador que encierre la cuenta en una carpeta que solo
contenga lo que se puede sincronizar: `ChrootDirectory` en OpenSSH, `DefaultRoot`
en ProFTPD. Es necesario en todos los servidores, porque un enlace puesto en la
carpeta puede hacer que la sincronización lea otro archivo que la cuenta pueda
leer. Un chroot que contiene también otros archivos los sigue exponiendo. Consulta
[qué transfiere una sincronización SFTP](#4-what-an-sftp-sync-transfers).

Prefiere una clave a una contraseña, y prefiere una clave Ed25519. Crea un par de
claves para la fuente:

```bash
ssh-keygen -t ed25519 -C agenticos-sync -f agenticos_sync
```

Si el servidor no acepta una clave Ed25519, crea en su lugar una clave ECDSA.
Apache MINA SSHD sin su biblioteca EdDSA es un servidor así.

```bash
ssh-keygen -t ecdsa -b 256 -C agenticos-sync -f agenticos_sync
```

El administrador añade `agenticos_sync.pub` al `authorized_keys` de la cuenta. La
clave privada, `agenticos_sync`, va al vault.

Añade el login al vault para el servicio **SFTP server**, como **SSH login**:

- **User name**: la cuenta en el servidor.
- **Host**: dónde se puede usar el login, como `sftp.example.com`. Para un puerto
  distinto del 22, añade el puerto: `sftp.example.com:2222`. Escribe una dirección
  IPv6 entre corchetes, como `[2001:db8::7]:2222`. El vault guarda el host tal
  como lo escribe una fuente: en minúsculas, sin punto final, una dirección IPv6 en
  su forma corta, y un nombre con letras que no son ASCII en su forma codificada,
  como `xn--bcher-kva.example` para `bücher.example`. Un nombre puede contener un
  guion bajo, como `sftp_server`. El host de un token de acceso de Git no puede.
- **Private key**: el archivo de clave entero, en formato OpenSSH, PKCS#8 o
  PKCS#1, de como mucho 16.384 caracteres. Si la clave está cifrada, da también su
  **Key passphrase**, de como mucho 1.024 caracteres. Una clave RSA puede tener como mucho 4096 bits, y sus dos primos tienen que ser del mismo
  tamaño, como los crea `ssh-keygen`. No se puede usar una clave guardada en una
  llave de seguridad (`sk-`). Una clave cifrada puede pedir como mucho
  128 rondas de bcrypt (OpenSSH), o 1.000.000 de iteraciones y una clave derivada
  de 64 bytes (PKCS#8). `ssh-keygen` usa 16 rondas.
- **Password**: solo para un servidor que no acepta ninguna clave. Tiene que tener
  al menos ocho caracteres y como mucho 1.024. Un servidor que solo acepta una contraseña mediante un
  login keyboard-interactive, como PAM con `PasswordAuthentication no`, la
  rechaza. El worker nunca responde a una petición keyboard-interactive, así que
  ahí usa una clave.

Un login guarda una clave privada o una contraseña, nunca ambas. El vault abre la
clave con su passphrase cuando guardas el login, así que una clave que no se abre
se rechaza en el acto, no en la primera sincronización. Abre la clave en un
proceso aparte que puede tardar como mucho 5 segundos. Una clave Ed25519 tarda
milisegundos. Una clave RSA de 4096 bits, o una clave en los límites de arriba,
tarda menos de dos segundos. Después elige el login en el paso de credencial de la
fuente.

**El host es del login, no de la fuente.** Quien edita una fuente escribe su host,
y el connector solo envía el login al host y al puerto con los que se añadió. Un
login añadido para `sftp.example.com` solo se usa en el puerto 22. Así que editar
una fuente no puede enviar el login de la organización a otro servidor.

!!! warning "Quien edita la fuente también elige la clave de host en la que confía"

    Quien edita la fuente y además puede interceptar el tráfico del worker hacia
    el host del login puede fijar una clave propia y recibir una contraseña. Un
    login con clave no expone la clave: el servidor solo recibe una firma que vale
    para su propia sesión. Este es el motivo para preferir una clave.

### 3. Campos de configuración del connector de SFTP { #3-sftp-connector-config-fields }

| Campo | Tipo | Obligatorio | Valor por defecto | Descripción |
|-------|------|----------|---------|-------------|
| `host` | string | Sí | -- | El nombre de host o la dirección IP del servidor, por ejemplo `sftp.example.com`. Solo el host: sin esquema, nombre de usuario, puerto ni ruta. |
| `port` | integer | No | `22` | El puerto SSH del servidor, de `1` a `65535`. |
| `root_path` | string | Sí | -- | La ruta absoluta de la carpeta que se lee, por ejemplo `/outgoing`. No tiene valor por defecto: en un servidor que no mantiene la cuenta en su carpeta personal, `/` es el sistema de archivos entero. |
| `host_key` | string | Sí | -- | La clave pública de host del servidor, una línea como `ssh-ed25519 AAAA...`, del [paso 1](#1-get-the-servers-host-key). Un comentario detrás de la clave se descarta. |
| `include_subfolders` | boolean | No | `true` | Si se leen las carpetas que hay bajo `root_path`. |
| `extensions` | lista de strings | No | `.pdf`, `.docx`, `.md`, `.txt` | Qué tipos de archivo se ingieren, como mucho 100, cada uno de como mucho 16 caracteres. Añade `.pptx`, `.xlsx` u otro tipo solo si el parser de la colección lo lee: un archivo que el parser no puede leer cuenta como archivo fallido en cada sincronización. |

El formulario muestra estos campos como **Host**, **Port**, **Folder**, **Host
key**, **Include subfolders** y **File types**. El connector escribe cada carpeta
de una sola forma: `/outgoing//2026/` y `/outgoing/./2026` pasan a ser
`/outgoing/2026`. Una carpeta con `..` se rechaza.

Cada archivo es un documento cuya dirección es `sftp://<user>@<host>:<port><path>`,
como `sftp://sync@sftp.example.com:22/outgoing/2026/report.pdf`. El nombre de
usuario sale del login y va codificado con porcentajes. La ruta es la ruta completa
del archivo en el servidor. Por eso dos cuentas de un mismo servidor mantienen
documentos separados, y una fuente cambiada a una carpeta más estrecha conserva
los documentos que sigue listando. Un login cambiado a otro nombre de usuario, o
una fuente cambiada a otro host o puerto, cambia todas las direcciones. La
siguiente sincronización vuelve entonces a leer todos los archivos, y un listado
completo elimina los documentos de las direcciones antiguas. Planifica un cambio
de servidor teniendo en cuenta ese coste.

### 4. Qué transfiere una sincronización SFTP { #4-what-an-sftp-sync-transfers }

SFTP no tiene feed de cambios, así que cada sincronización lista el árbol de
carpetas entero. El listado da el tamaño y la hora de modificación de cada
archivo, en segundos enteros. Ese par es la revisión del archivo, así que
`new_only` y `update_only` omiten un archivo cuyo tamaño y hora no han cambiado,
antes de la descarga.

!!! warning "Se pasa por alto una edición que conserva el tamaño y la hora"

    `put -p` conserva la hora de quien sube el archivo, un `SETSTAT` puede volver
    a poner una hora anterior, y una reescritura del mismo tamaño puede caer dentro
    de un mismo segundo. Cada una de ellas cambia un archivo y conserva su
    revisión, así que `new_only` y `update_only` omiten el archivo. Ejecuta la
    fuente en modo `full` para volver a leer todos los archivos. OpenSSH, SFTPGo,
    ProFTPD y Apache MINA SSHD se comportan todos así.

Una descarga lee el archivo en peticiones de 255 KiB, de una en una, así que un
archivo de 50 MB cuesta unos 200 viajes de ida y vuelta. El connector compara los
atributos (stat) del archivo antes y después de la lectura. Un archivo que cambió
o se sustituyó durante la lectura, o que se borró después del listado, se cuenta
como `skipped`. La siguiente sincronización lee su versión nueva o elimina su
documento.

Una subida que sigue en curso suele cambiar el archivo durante la lectura, así que
la sincronización omite el archivo. Si no llega ningún byte durante la lectura, la
sincronización puede ingerir la parte que ya ha llegado, y la siguiente
sincronización lee el archivo completo. Una subida con un nombre temporal,
renombrada al completarse, nunca se lee a medias si el nombre temporal tiene un
tipo que no está en **File types**, como `.part` o `.filepart`.

Un archivo que supera el límite de documento de la base de conocimiento
(`MAX_UPLOAD_SIZE_MB`, 50 MB por defecto) no se descarga. Cuenta como archivo
fallido en cada sincronización, así que sigue a la vista. El connector también
cuenta los bytes durante la lectura, así que un servidor que envía más de lo que
declaraban sus atributos no puede saltarse el límite.

El listado deja fuera estas entradas:

- **Enlaces y archivos especiales.** El listado nunca lista un enlace simbólico,
  apunte a donde apunte, así que la sincronización no recorre ningún bucle. Un
  FIFO, un socket o un dispositivo tampoco se listan. Aun así, un enlace puede
  sustituir a un archivo listado, o a una carpeta, después del listado: consulta
  el aviso de más abajo.
- **Nombres que no pueden ser una dirección.** Un nombre vacío, que contiene `/` o
  un byte NUL, que ocupa más de 255 bytes o que no es UTF-8, se deja fuera.
  También un nombre cuya ruta ocupa más de 4.096 bytes. El registro de
  sincronización nombra la carpeta, y el listado sigue completo.
- **Entradas de tipo desconocido.** Una entrada cuyo tipo no da el servidor se
  deja fuera. El listado queda entonces incompleto, así que ese run no elimina
  nada.
- **Carpetas que el servidor no lista.** Una carpeta bajo `root_path` que el
  servidor se niega a listar, normalmente por falta de permiso, o sobre la que
  responde de una forma que el worker no puede descodificar, se nombra en el
  registro de sincronización y en el log del worker. Ese run no elimina nada.

!!! warning "Un enlace puesto en la carpeta puede hacer que la sincronización lea otro archivo"

    La versión 3 de SFTP no puede abrir una ruta sin seguir un enlace, y ningún
    servidor probado ofrece una forma de evitarlo. Quien puede escribir en la
    carpeta puede sustituir un archivo listado por un enlace después del listado.
    La descarga lee entonces el destino del enlace, si la cuenta puede leerlo, y
    la sincronización lo ingiere como el archivo listado.

    Esta carrera existe en todos los servidores. Donde el stat del servidor
    informa de un enlace como enlace, como en OpenSSH, SFTPGo y Apache MINA SSHD,
    el enlace tiene que ponerse durante unas pocas peticiones antes de la
    descarga, y quitarse de nuevo antes de que la descarga termine. La revisión
    guardada es entonces la del archivo listado, así que `new_only` y
    `update_only` conservan el contenido equivocado hasta que cambia el archivo
    listado. En ProFTPD, cuyo stat sigue los enlaces, la ventana va del listado a
    la descarga, y el enlace puede quedarse.

    Una carpeta se puede sustituir de la misma forma, en todos los servidores, y
    ese enlace puede quedarse. Un stat de una ruta sigue un enlace en cada carpeta
    de la ruta, y solo comprueba el último nombre. Si alguien sustituye una
    carpeta por un enlace después de que la sincronización listara la carpeta de
    arriba, la sincronización lista en su lugar el destino del enlace, e ingiere
    esos archivos bajo la ruta de la carpeta. Ninguna comprobación del connector
    lo ve.

    La solución está en el servidor:

    - La cuenta solo puede leer lo que se puede sincronizar: un chroot cuyo
      contenido es el árbol sincronizado (`ChrootDirectory` con
      `ForceCommand internal-sftp` en OpenSSH, `DefaultRoot` en ProFTPD), o una
      cuenta cuyos permisos solo alcanzan ese árbol. Un chroot que contiene
      también otros archivos los sigue exponiendo a través de un enlace. En Apache
      MINA SSHD, una raíz virtual no detiene un enlace, así que encierra la cuenta
      en el sistema operativo. SFTPGo rechaza un enlace que apunta fuera de la
      raíz del usuario.
    - Las cuentas que suben archivos no pueden crear enlaces ni enlaces duros:
      `internal-sftp -P symlink,hardlink` en OpenSSH, `<Limit SYMLINK> DenyAll`
      y `SFTPExtensions -hardlink` en ProFTPD, y ningún permiso
      `create_symlinks` en SFTPGo. En ProFTPD, `<Limit SYMLINK>` solo no detiene
      un enlace duro.
    - Un enlace duro supera todas las comprobaciones y un chroot. El ajuste de
      Linux `fs.protected_hardlinks=1`, el valor por defecto en las distribuciones
      habituales, impide que un usuario enlace un archivo que no es suyo y que no
      puede leer y escribir.

    El connector también comprueba que el archivo que abrió es el archivo que
    listó. El propietario y el grupo tienen que ser los del listado. Los permisos
    tienen que ser los del stat justo antes de la apertura, y también los del
    listado cuando ese stat da otro tamaño u otra hora de modificación que el
    listado. Los permisos de un listado se pueden falsear, como hace
    `DirFakeMode` de ProFTPD, así que ahí un archivo sin cambios se sigue
    leyendo. Un archivo que de verdad cambió en un servidor así falla una vez, y
    la siguiente sincronización lo lee.

    Esto es defensa en profundidad, no una solución, y solo cubre un archivo
    sustituido. En OpenSSH y ProFTPD detecta un cambio a un archivo con otro
    propietario, grupo o permisos, y el archivo falla con "The server opened a
    file for … that is not the file it listed. …".

    La comprobación no hace nada en Apache MINA SSHD ni en SFTPGo, que la
    responden a partir de la ruta. Tampoco detecta un enlace duro, un destino con
    el mismo propietario, grupo y permisos, ni una carpeta sustituida por un
    enlace antes de que la sincronización la listara. En ProFTPD tampoco detecta
    un enlace puesto antes del stat hacia un archivo con el propietario, el grupo,
    el tamaño y la hora de modificación del listado.

El log del worker es la salida del proceso worker. En un deployment con compose,
léelo con `docker compose logs worker`. Contiene la respuesta del propio servidor,
que el registro de sincronización nunca muestra, y el motivo por el que falló un
archivo.

Cada frase que el listado añade al registro de sincronización cuenta como un
archivo fallido. Así que el run tiene el estado `error` hasta que se elimina la
causa en el servidor, o hasta que una carpeta más estrecha la deja fuera. El
registro de sincronización muestra como mucho tres notas, y cuenta las demás como
"And … more.". Corrige las causas que muestra y vuelve a sincronizar para ver las
siguientes.

Un carácter de control en el nombre de un archivo o de una carpeta, como un salto
de línea o un separador Unicode de línea o de párrafo (U+2028, U+2029), se muestra
como `?` en el registro de sincronización y en las frases propias del connector en
el log del worker. El archivo se sigue sincronizando con su propio nombre. La
línea `Failed to sync …` del worker muestra el nombre tal como lo escapa el filtro
del log, como `\n` para un salto de línea.

Una sincronización usa una sola conexión para el listado y todas las descargas. El
worker envía un keepalive cada 30 segundos, y un servidor que no responde a tres
se da por desaparecido. Una conexión o una sesión SFTP que el servidor cerró
entre dos archivos se vuelve a abrir para el siguiente archivo.

El listado lee hasta 4 carpetas a la vez por esa conexión, con unos 4 viajes de ida
y vuelta por carpeta. Un archivo pequeño cuesta entonces unos 7 viajes de ida y
vuelta: dos stats de la ruta, la apertura, dos stats del archivo abierto, una
lectura y el cierre. A 80 ms por viaje de ida y vuelta, eso es más o menos medio
segundo por archivo, antes del parseo y del embedding.

Una sincronización puede durar 4 horas, así que una primera importación de un árbol
grande por una conexión lenta puede necesitar más de un run. Cada run conserva lo
que ingirieron los runs anteriores, y `new_only` y `update_only` omiten esos
archivos antes de la descarga, así que el siguiente run sigue donde se detuvo el
anterior.

Un intento de conexión que falla no se repite en la misma sincronización. Esto
incluye un login rechazado, un timeout y una conexión perdida antes de que
empezara la sesión SFTP. Cada archivo posterior falla con la misma frase, y no se
envía un segundo login. Así que una contraseña cambiada cuesta un login rechazado
por sincronización, no uno por archivo. Cada sincronización sigue enviando uno, así
que un horario corto o sincronizaciones manuales repetidas pueden alcanzar el
límite de bloqueo del servidor. Cuando el login se rechaza, corrígelo o pausa la
fuente.

!!! warning "Un login erróneo puede detener las demás fuentes en un servidor OpenSSH"

    OpenSSH 9.8 y posteriores rechazan durante un tiempo las conexiones desde una
    dirección después de logins fallidos desde ella (`PerSourcePenalties`, activado
    por defecto). Todas las fuentes de un deployment suelen conectarse desde una
    sola dirección, la de los workers. Así que una fuente con una contraseña
    errónea puede hacer que las sincronizaciones de otras fuentes del mismo
    servidor fallen con [se perdió la conexión](#sftp-the-connection-to-was-lost),
    aunque sus propios logins sean correctos.

### Qué acota una sincronización SFTP { #what-bounds-an-sftp-sync }

Unos límites fijos protegen al worker de un servidor roto u hostil. No son
ajustes. Un listado que alcanza uno de los seis primeros se queda corto, nombra
el límite en el registro de sincronización y no elimina nada. Elige una carpeta más
estrecha hasta que una sincronización termine sin el mensaje.

| Límite | Valor | Cuando se alcanza |
|---|---|---|
| Niveles de carpeta bajo `root_path` | 32 | El listado se detiene |
| Carpetas en el listado | 10.000 | Las carpetas que pasan de ahí no se leen |
| Nombres en el listado, `.` y `..` incluidos | 1.000.000 | El listado se detiene |
| Archivos de los tipos configurados en el listado | 100.000 | El listado se detiene |
| La memoria que ocupan las rutas de los archivos listados | 64 MB, más o menos un byte por carácter en una ruta ASCII y hasta cuatro para otros caracteres | El listado se detiene |
| El listado entero | 30 minutos | El listado se detiene |
| Un nombre, y una ruta | 255 bytes, y 4.096 bytes | El nombre se deja fuera |
| La conexión y el login | 30 segundos | La conexión falla |
| Una petición SFTP | 30 segundos | El worker corta la conexión. Una petición del listado hace fallar la sincronización, y una petición de una descarga hace fallar ese archivo |
| La descarga de un archivo | 10 minutos | El archivo falla |
| El tamaño de un archivo | `MAX_UPLOAD_SIZE_MB`, 50 MB por defecto | El archivo falla |

### Reglas de red de SFTP { #sftp-network-rules }

El host de la fuente se resuelve y se comprueba como cualquier otra dirección que
elige un inquilino. Un host que resuelve a una dirección privada, de loopback,
link-local, CGNAT o reservada se rechaza al guardar la fuente y de nuevo al
sincronizar. Después, el worker solo se conecta a las direcciones que pasaron la
comprobación, y nunca resuelve el nombre una segunda vez.

Un servidor de archivos de la empresa tiene una dirección privada. Un operador
puede listarlo en `SYNC_ALLOWED_HOSTS` (consulta [ajustes del
proyecto](../configuration.md#project-settings)). Configúralo para la API y para
el worker. La API resuelve y comprueba el host al guardar una fuente, y el worker
lo hace de nuevo al sincronizarla, así que los dos tienen que poder resolver
también el nombre.

Un host de la lista puede resolver a una dirección privada o CGNAT. Nunca puede
resolver a una de loopback, link-local, multicast o de metadatos de la nube. La
lista vale para todo el deployment: las fuentes de cualquier organización
pueden llegar a cualquier puerto de un host listado. Solo la leen las fuentes SFTP. Las fuentes de Git y
de sitio web mantienen la comprobación de dirección sin excepciones.

Escribe cada entrada como el **Host** de una fuente: solo un nombre o una dirección
IP. La lista compara el host que escribe la fuente, no la dirección a la que
resuelve. Para una fuente cuyo **Host** es `files.corp.example`, lista
`files.corp.example`: su dirección `10.0.0.5` sola no la permite. Una entrada con
un puerto, un esquema, una ruta, un rango, un comodín o un
punto final impide que se carguen los ajustes, y el error de arranque nombra la
entrada. Lo mismo hace una entrada que se lee como un host de loopback, link-local
o de metadatos una vez normalizada como el host de una fuente, como `localhost` en
letras de ancho completo.

Cualquier organización puede añadir un login propio para un host de la lista e
intentarlo allí. Una organización sin un login válido sigue sabiendo si el
host responde a SSH y si su clave de host coincide. Así que lista solo un servidor
cuyas cuentas sean el control de acceso.

El worker se conecta directamente al puerto SSH del servidor. Un proxy HTTP como
`HTTPS_PROXY` no se le aplica.

El worker no lee nada de su propia configuración SSH:

- `~/.ssh/config`, `~/.ssh/known_hosts` y las claves de `~/.ssh`
- un agente SSH en `SSH_AUTH_SOCK`
- un ticket de Kerberos, cuyo intercambio de claves se salta la comprobación de la
  clave de host

La clave fijada es la única clave de host en la que confía el worker. El worker
solo prueba el único método de login que guarda el login del vault, y nunca
keyboard-interactive. Un servidor que deja entrar al worker sin login se rechaza,
porque ese servidor no comprobó nada.

La compresión está desactivada. asyncssh no pone límite a lo que puede ocupar un
paquete comprimido al descomprimirse, así que un servidor podría hacer que el
worker usara cientos de megabytes por cada paquete. Un servidor que solo ofrece
conexiones comprimidas se rechaza con [la frase del intercambio de
claves](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher).

!!! note "Un worker necesita un nombre de usuario local"

    asyncssh llama a `getpass.getuser()` en cada conexión, y nada lo desactiva. El
    nombre no se envía. La imagen que se distribuye tiene una entrada en passwd
    para su usuario. Un worker que se ejecuta como un usuario sin entrada en
    passwd necesita la variable `LOGNAME`. Esto vale para una imagen propia, y
    para la imagen que se distribuye arrancada con otro id de usuario, por ejemplo
    con `docker run --user` o un `runAsUser` de Kubernetes. Sin ella, cada
    sincronización SFTP falla, y el registro de sincronización muestra "The sync
    did not finish (OSError) - check the source's credential, then run it again.
    …". La credencial no es la causa.

## Referencia de la API { #api-reference }

Todos los endpoints de las fuentes de sincronización viven bajo
`/api/v1/rag/sync/`. Listar exige `collections:view` y todo lo que modifica una
fuente exige `collections:edit`, en ambos casos sobre la colección a la que
pertenece la fuente: no interviene ningún rol de administrador. Consulta
[quién puede llegar a una colección](../file-processing.md#who-may-reach-a-collection).

### CRUD de fuentes de sincronización { #sync-sources-crud }

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/sources` | Lista todas las fuentes de sincronización configuradas |
| `POST` | `/api/v1/rag/sync/sources` | Crea una fuente de sincronización nueva |
| `PATCH` | `/api/v1/rag/sync/sources/{id}` | Modifica una fuente de sincronización existente |
| `DELETE` | `/api/v1/rag/sync/sources/{id}` | Borra una fuente de sincronización |
| `POST` | `/api/v1/rag/sync/sources/{id}/trigger` | Lanza una sincronización a mano |

### Connectores y registros { #connectors-logs }

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/connectors` | Lista los tipos de connector disponibles con sus esquemas de configuración |
| `GET` | `/api/v1/rag/sync/logs` | Lista el historial de sincronizaciones (se puede filtrar por `collection_name`) |

### Ejemplo: crear una fuente con la API { #example-create-a-source-via-api }

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Legal Drive",
    "connector_type": "gdrive",
    "collection_name": "legal",
    "config": {
      "folder_id": "1abc123def",
      "include_subfolders": true
    },
    "sync_mode": "new_only",
    "schedule_minutes": 120
  }'
```

### Ejemplo: lanzar una sincronización con la API { #example-trigger-a-sync-via-api }

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources/{source_id}/trigger \
  -H "Authorization: Bearer $TOKEN"
```

### Ejemplo: consultar el historial de sincronizaciones { #example-check-sync-history }

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?limit=10 \
  -H "Authorization: Bearer $TOKEN"
```

### Ejemplo: descubrir los connectores disponibles { #example-discover-available-connectors }

```bash
curl http://localhost:8000/api/v1/rag/sync/connectors \
  -H "Authorization: Bearer $TOKEN"
```

La respuesta incluye el `config_schema` de cada connector, que el frontend usa
para renderizar formularios dinámicos. También resulta útil para construir
integraciones de forma programática.

## Modificar una fuente { #updating-a-source }

Puedes actualizar cualquier subconjunto de campos de una fuente existente con
`PATCH`:

```bash
curl -X PATCH http://localhost:8000/api/v1/rag/sync/sources/{source_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sync_mode": "full",
    "schedule_minutes": 60,
    "is_active": false
  }'
```

Campos modificables: `name`, `config`, `sync_mode`, `schedule_minutes`,
`is_active`, `collection_name`.

Pon `is_active` en `false` para pausar una fuente sin borrarla.

## Supervisar las sincronizaciones { #monitoring-sync-operations }

Cada sincronización crea una entrada de `SyncLog` con estos campos:

| Campo | Descripción |
|-------|-------------|
| `source` | El tipo de connector, o `"local"` para la ingesta desde la CLI |
| `collection_name` | La colección de destino |
| `status` | `running`, `done` o `error` |
| `mode` | `full`, `new_only` o `update_only` |
| `total_files` | Número de archivos encontrados |
| `ingested` | Ingestados correctamente (nuevos) |
| `updated` | Reingestados correctamente (reemplazados) |
| `skipped` | Omitidos: ya presentes o sin cambios, nuevos en `update_only`, o cambiados o eliminados durante el run y dejados para el siguiente run |
| `failed` | No se pudieron ingestar, incluidas las páginas o archivos que el listado no pudo leer y los documentos que no se pudieron eliminar |
| `removed` | Eliminados porque la fuente ya no los lista (consulta [qué elimina una sincronización](#what-a-sync-removes)) |
| `error_message` | Qué salió mal, o por qué no se eliminó nada. Un run puede estar en `done` y aun así tener un mensaje, por ejemplo cuando un crawl se detuvo en su límite de páginas, o cuando algunos archivos cambiaron o se eliminaron durante el run y quedaron para el siguiente run. Lee `status` para saber si el run falló |
| `started_at` | Cuándo empezó la sincronización |
| `completed_at` | Cuándo terminó la sincronización |

Consulta los registros desde la salida de la CLI o desde la API:

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?collection_name=legal&limit=5 \
  -H "Authorization: Bearer $TOKEN"
```

## Añadir connectores propios { #adding-custom-connectors }

Para añadir un tipo de connector nuevo (por ejemplo, Notion, Confluence o
Dropbox), consulta
[Añade un connector de sincronización](./add-sync-connector.md).

La versión corta:

1. Crea una clase que herede de `BaseSyncConnector` en
   `app/services/rag/connectors/`.
2. Implementa `list_files()`, `_fetch()` y, opcionalmente,
   `validate_config()`.
3. Declara `SECRET_KIND` —qué tipo de secreto del vault lo autentica— y un
   `CONFIG_MODEL`, un modelo de Pydantic que dice cómo encontrar los documentos.
   La credencial nunca es uno de sus campos.
4. Regístralo en `CONNECTOR_REGISTRY`, en
   `app/services/rag/connectors/__init__.py`.

Una vez registrado, el connector aparece por sí solo en la CLI, en la API y en
la interfaz.

## Resolución de problemas { #troubleshooting }

### "No sync sources configured" { #no-sync-sources-configured }

Todavía no has creado ninguna fuente. Crea una con `rag-source-add` (CLI) o con
`POST /api/v1/rag/sync/sources` (API).

### "Unknown connector type" { #unknown-connector-type }

El tipo de connector que has indicado no está en `CONNECTOR_REGISTRY`. Consulta
los tipos disponibles con `rag-sources` o con
`GET /api/v1/rag/sync/connectors`.
Google Drive (`gdrive`) está disponible.
S3 (`s3`) está disponible.
Website (`web`) está disponible.
Git (`git`) está disponible.
SFTP (`sftp`) está disponible.

### Google Drive: "this source has no credential" { #google-drive-this-source-has-no-credential }

El `secret_id` de la fuente está vacío, o el secreto del vault que nombraba ha
sido borrado. Guarda el JSON de la cuenta de servicio en el vault y elígelo en el
paso de credencial de la fuente: `GOOGLE_DRIVE_CREDENTIALS_FILE` no lo sustituye,
y solo el comando de CLI `rag-sync-gdrive` lee ese ajuste.

### "A Google Drive source needs a service account credential" { #a-google-drive-source-needs-a-service-account-credential }

El `secret_id` nombra una credencial del tipo equivocado: un par de claves de
AWS, por ejemplo. Una fuente de Drive toma un `gcp_service_account` y una fuente
de S3 un par `aws_credentials`; el asistente solo ofrece las que corresponden,
así que a esto se llega a través de la API.

### Google Drive: "folder ID may contain only letters, digits, '-' and '\_'" { #google-drive-folder-id-may-contain-only-letters-digits-and-_ }

El valor no es un id de carpeta de Drive. Tómalo de la URL de la carpeta: es el
último segmento, y nada más de esa URL pertenece al campo.

### Google Drive: "Cannot access folder" { #google-drive-cannot-access-folder }

Asegúrate de haber compartido la carpeta con el correo de la cuenta de servicio.
La cuenta de servicio necesita al menos acceso Viewer.

### S3: "Cannot access bucket" { #s3-cannot-access-bucket }

Comprueba que `S3_RAG_ACCESS_KEY`, `S3_RAG_SECRET_KEY` y `S3_RAG_ENDPOINT` están
bien puestos en el `.env`. En MinIO, asegúrate de que el endpoint incluye el
puerto (por ejemplo, `http://localhost:9000`).

### Sitio web: "resolves to private/internal address" { #website-resolves-to-privateinternal-address }

La URL de inicio, o el sitemap, apunta dentro de la red del deployment, o su
nombre resuelve ahí. Una fuente de sitio web solo lee direcciones públicas. Para
indexar un sitio interno, publica sus páginas en algún lugar público o sube los
archivos directamente.

### Sitio web: "The site's robots.txt could not be read, so it was not crawled" { #website-the-sites-robotstxt-could-not-be-read-so-it-was-not-crawled }

`/robots.txt` en el host de la URL de inicio agotó el tiempo de espera o
respondió con un error del servidor (5xx) tres veces seguidas. El crawler no
adivina qué permitiría un robots.txt inaccesible, así que el run se detiene. Un
robots.txt que falta (404) o que está prohibido (403) significa que no hay
reglas, y el crawl sigue adelante.

### Sitio web: "The start URL … did not lead to an HTML page" { #website-the-start-url-did-not-lead-to-an-html-page }

La URL de inicio respondió 404, redirigió a otro host o fuera de `path_prefix`,
o sirvió algo que no es HTML. Ábrela en un navegador y usa como `root_url` la
dirección en la que acaba.

### Sitio web: "robots.txt does not allow the start URL" { #website-robotstxt-does-not-allow-the-start-url }

El sitio pide a los crawlers que no entren en esa ruta. Elige una URL de inicio
que el sitio permita, o pide al propietario del sitio que permita
`AgenticOS-Crawler`.

### Sitio web: "… answered HTTP 403" o "… could not be reached" { #website-answered-http-403-or-could-not-be-reached }

La página necesita un login, o el sitio rechazó al crawler. Falló tras tres
intentos si la respuesta fue un timeout, un 429 o un 5xx. Cada página así cuenta
como un archivo fallido. En ese run no se elimina nada, porque no se vieron las
páginas que había detrás.

### "The source could not be listed completely, so documents it may no longer hold were kept" { #the-source-could-not-be-listed-completely-so-documents-it-may-no-longer-hold-were-kept }

El listado se quedó corto: un crawl alcanzó `max_pages`, algunas páginas no se
pudieron leer, una carpeta de SharePoint no se pudo listar, o un listado SFTP
alcanzó un límite o no pudo leer una carpeta o una entrada. Lo que se encontró se
ingestó, y no se eliminó nada. Para un sitio web, sube `max_pages`, o acota el
crawl con `path_prefix`, hasta que un run termine sin este mensaje. Para
SharePoint y SFTP, consulta más abajo las entradas sobre el listado.

### Git: "The repository refused the source's token" { #git-the-repository-refused-the-sources-token }

El token ha caducado, se ha revocado o no puede leer este repositorio. Emite uno
nuevo como se describe en [Configurar un repositorio Git](#git-repository-setup)
y sustituye el valor del secreto del vault que usa la fuente; todas las fuentes
que usan ese secreto lo recogen en su siguiente sincronización.

### Git: "The repository was not found, or the source's token cannot see it" { #git-the-repository-was-not-found-or-the-sources-token-cannot-see-it }

Comprueba primero la URL de clonado. Un repositorio privado responde *not found*
en lugar de *forbidden* a un token que no puede leerlo, así que un token
fine-grained emitido para otro repositorio se ve así.

### Git: "The repository has no branch named …" { #git-the-repository-has-no-branch-named }

El campo `branch` nombra una rama que el repositorio no tiene. Su valor por
defecto es `main`; la rama por defecto de un repositorio más antiguo puede ser
`master`.

### Git: "… is … MB, and a synced file may be at most … MB" { #git-is-mb-and-a-synced-file-may-be-at-most-mb }

Un archivo que casa con los patrones de inclusión supera el límite de documento
de la base de conocimiento. Acota `include` o `path_prefix` para que ese archivo
quede fuera. En esta sincronización no se escribió nada.

### Git: "… over the … MB one sync may check out" { #git-over-the-mb-one-sync-may-check-out }

Todos los archivos que casan con los patrones de inclusión suman más de 512 MB.
Acota `include` o `path_prefix`, o divide el repositorio en varias fuentes, cada
una con su propio prefijo.

### Git: "This token was added for …, and the repository is on …" { #git-this-token-was-added-for-and-the-repository-is-on }

El repositorio de la fuente está en un host distinto de aquel con el que se añadió
su token. O la URL es incorrecta, o la fuente necesita un token añadido para ese
host. No se envió nada al host del repositorio.

### Git: "A Git source needs a Git access token" { #git-a-git-source-needs-a-git-access-token }

La fuente nombra un secreto de otro kind, como una API key. Añade el token como
**Git access token**, con su host, y elige ese.

### "Another sync of this source is still running" { #another-sync-of-this-source-is-still-running }

Se lanzó una ejecución mientras había otra de la misma fuente en curso, así que no
arrancó. La ejecución en curso termina con normalidad; vuelve a lanzarla después
si la fuente ha cambiado entretanto.

### Git: "… resolves to a private address" { #git-resolves-to-a-private-address }

El host del repositorio resuelve dentro de la red del deployment, así que la
fuente se rechaza. Una fuente de sincronización no puede llegar a un servidor Git
autoalojado en una dirección interna.

### Git: "git is not installed on this worker" { #git-git-is-not-installed-on-this-worker }

El worker se ejecuta desde una imagen sin `git`. El `backend/Dockerfile` que se
distribuye lo instala; a una imagen propia hay que añadírselo.

### Git: una sincronización terminó sin ningún archivo procesado { #git-a-sync-finished-with-no-files-processed }

El commit de cabeza de la rama es el mismo que leyó la última ejecución limpia,
con la misma configuración, así que no había nada que hacer. Cambia la fuente a
`full` durante una ejecución para volver a leerlo todo de todos modos.

### SharePoint: "Microsoft Entra refused the app registration's credentials (…)" { #sharepoint-microsoft-entra-refused-the-app-registrations-credentials }

Microsoft Entra no inició la sesión de la app. `invalid_client` suele significar
que el client secret ha caducado o que el vault guarda el ID del secreto en lugar
de su valor. `unauthorized_client` o `invalid_request` suele significar que el id
del tenant o el client id son incorrectos. Comprueba los tres valores con la
**Overview** y **Certificates & secrets** de la app, y sustitúyelos en el secreto
del vault.

### SharePoint: "Microsoft Graph did not accept the app's token for …" { #sharepoint-microsoft-graph-did-not-accept-the-apps-token-for }

La app inició sesión, pero su token no lleva ningún permiso de Microsoft Graph.
Añade el permiso de **aplicación** `Sites.Selected`, no uno delegado, y concede el
consentimiento de administrador, como se describe en el
[paso 2](#2-grant-it-one-site-not-the-tenant).

### SharePoint: "Microsoft Graph denied the app access to … (accessDenied)" { #sharepoint-microsoft-graph-denied-the-app-access-to-accessdenied }

La app tiene `Sites.Selected` pero ninguna concesión sobre este sitio. Concédele
`read` sobre el sitio, como se describe en el
[paso 2](#2-grant-it-one-site-not-the-tenant). Si el mensaje nombra una carpeta
bajo la carpeta de la fuente, esa carpeta tiene permisos propios que excluyen a la
app. El resto de la biblioteca se ingirió y no se eliminó nada.

### SharePoint: "There is no SharePoint site at …, or the app cannot see it" { #sharepoint-there-is-no-sharepoint-site-at-or-the-app-cannot-see-it }

Comprueba primero la URL del sitio: es la dirección del propio sitio, como
`https://contoso.sharepoint.com/sites/HR`, no una biblioteca ni una página dentro
de él. Con `Sites.Selected`, un sitio sobre el que la app no tiene concesión
también puede responder así.

### SharePoint: "The site has no document library named …" { #sharepoint-the-site-has-no-document-library-named }

El mensaje lista las bibliotecas que la app puede ver. Copia uno de esos nombres
en `library`, o deja `library` vacío para la biblioteca por defecto del sitio. La
biblioteca por defecto lleva el nombre en el idioma del sitio, como *Documents* o
*Dokumente*.

### SharePoint: "The library has no folder …" o "… is a file, not a folder" { #sharepoint-the-library-has-no-folder-or-is-a-file-not-a-folder }

`folder_path` no nombra una carpeta de la biblioteca. Es relativo al nivel
superior de la biblioteca, por ejemplo `Policies/HR`, y no repite el nombre de la
biblioteca.

### SharePoint: "The folder … could not be listed: …" { #sharepoint-the-folder-could-not-be-listed }

No se pudo leer una carpeta bajo la carpeta de la fuente. El motivo va detrás de
los dos puntos. Las demás carpetas se ingirieron, y en ese run no se eliminó nada.

### SharePoint: "Microsoft 365 stayed unavailable or kept throttling … after 4 attempts" { #sharepoint-microsoft-365-stayed-unavailable-or-kept-throttling-after-4-attempts }

Graph seguía limitando o fallando tras cuatro intentos. La siguiente
sincronización vuelve a empezar. Si ocurre en cada sincronización, programa la
fuente con menos frecuencia, o divide una biblioteca muy grande en varias fuentes,
cada una con su propio `folder_path`.

### SharePoint: "A SharePoint source needs a Microsoft Entra app credential" { #sharepoint-a-sharepoint-source-needs-a-microsoft-entra-app-credential }

La fuente nombra un secreto de otro kind, como una API key. Añade la app al vault
como **Microsoft Entra app** y elige esa.

### SharePoint: una sincronización terminó sin ningún archivo procesado { #sharepoint-a-sync-finished-with-no-files-processed }

Nada en la biblioteca ha cambiado desde la última ejecución limpia con la misma
configuración, así que no había nada que hacer. Cambia la fuente a `full` durante
una ejecución para volver a leerlo todo de todos modos.

### SFTP: "Paste only the key type and the key, without the host name or the options in front of them." { #sftp-paste-only-the-key-type-and-the-key-without-the-host-name-or-the-options-in-front-of-them }

**Host key** contiene una línea de `ssh-keyscan` o de `known_hosts`, con el nombre
del host delante de la clave, o una línea de `authorized_keys` con opciones
delante. Quita el primer campo. Para el archivo del
[paso 1](#1-get-the-servers-host-key), lo hace `cut -d' ' -f2- host_key.txt`.

### SFTP: "Paste the server's public host key as one line, with its type first, …" { #sftp-paste-the-servers-public-host-key-as-one-line-with-its-type-first }

**Host key** contiene algo que no es una única línea de clave pública: una huella
de `ssh-keygen -lf`, dos líneas, un bloque PEM o una clave dañada. Pega la clave
en sí, como `ssh-ed25519 AAAAC3Nz...`. Un bloque PEM se rechaza antes de leerlo,
porque una clave privada pegada por error no debe leerse.

### SFTP: "The host key must be of type ssh-ed25519, ssh-ed448, …" { #sftp-the-host-key-must-be-of-type-ssh-ed25519-ssh-ed448 }

La clave es de un tipo que el connector no fija, como `ssh-dss`, un certificado de
host o una llave de seguridad. Obtén del servidor una clave de uno de los tipos
nombrados, por ejemplo con `ssh-keyscan -t ed25519`, `-t ecdsa` o `-t rsa`.

### SFTP: "The host is a host name or an IP address alone, …" o "Put the port in the Port field, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-alone-or-put-the-port-in-the-port-field }

**Host** contiene más que el host, como `sftp://sftp.example.com/outgoing`,
`sync@sftp.example.com` o `sftp.example.com:2222`. Escribe solo el nombre o la
dirección. Pon el puerto en **Port** y la carpeta en **Folder**.

También se rechaza un punto final: quítalo. Un nombre solo contiene letras,
dígitos, guiones, guiones bajos y puntos, como `sftp_server` o
`files-01.corp.example`.

### SFTP: una carpeta o un tipo de archivo rechazados al guardar la fuente { #sftp-a-folder-or-a-file-type-refused-when-the-source-is-saved }

- "The folder is an absolute path on the server, e.g. /outgoing." **Folder** no
  empieza por `/`. Escribe la ruta completa tal como la ve la cuenta en el
  servidor.
- "The folder may not hold '..'. Name the folder itself." Escribe la ruta propia
  de la carpeta, sin `..`.
- "The folder may not hold a line break or another control character." Quita el
  carácter. Un pegado puede introducir uno, también un separador Unicode de línea
  o de párrafo (U+2028, U+2029).
- "The folder may be at most 1024 characters long." Elige una carpeta con una ruta
  más corta.
- "'…' is not a file extension, e.g. .pdf." Una entrada de **File types** no es un
  punto seguido de 1 a 10 letras o dígitos.
- "At least one file type is needed." **File types** está vacío. Añade al menos un
  tipo, como `.pdf`.
- "At most 100 file types can be listed." **File types** tiene más de 100
  entradas. Deja los tipos que lee el parser de la colección.
- "A file type is at most 16 characters, e.g. .pdf." Una entrada de **File types**
  tiene más de 16 caracteres. Escribe solo la extensión, como `.pdf`.

El campo **File types** de una fuente de SharePoint da las mismas cuatro últimas
frases. Los dos límites se aplican al guardar una fuente, y cuentan los tipos
después de quitar los vacíos y los repetidos. Una fuente guardada con más tipos se
sigue sincronizando, y solo se rechaza cuando se vuelve a guardar.

### SFTP: "Blocked: '…' resolves to private/internal address '…'. …" { #sftp-blocked-resolves-to-privateinternal-address }

El host resuelve dentro de la red del deployment. Un operador puede poner un
servidor de archivos de la red de la empresa en la lista `SYNC_ALLOWED_HOSTS`,
como se describe en [Reglas de red de SFTP](#sftp-network-rules). Un host que
resuelve a una dirección de loopback, link-local o de metadatos de la nube sigue
rechazado, también cuando está en la lista. Para un host escrito como dirección
IP, la frase es "Blocked: '…' resolves to a private/internal address. SSRF
protection does not allow requests to internal networks." La causa y la solución
son las mismas.

Lista el host tal como lo nombra la fuente, no la dirección de la frase. Configura
`SYNC_ALLOWED_HOSTS` para la API y para el worker. A una fuente rechazada al
guardarla le falta la entrada en la lista de la API. A una fuente rechazada solo al
sincronizarse le falta en la lista del worker.

"Blocked: unable to resolve hostname …" significa que el host no se pudo resolver
en absoluto. Comprueba cómo está escrito. La API resuelve el host cuando se guarda
la fuente, y el worker cuando la fuente se sincroniza, así que los dos tienen que
poder resolver el nombre.

### "A credential does not go in a source's configuration" { #a-credential-does-not-go-in-a-sources-configuration }

`config` contiene un campo que es una credencial, como `password`, `private_key` o
`passphrase` en una fuente SFTP. Añade la credencial al vault y apunta a ella el
`secret_id` de la fuente.

### SFTP: "An SSH login needs a private key or a password, and not both." { #sftp-an-ssh-login-needs-a-private-key-or-a-password-and-not-both }

El login tiene a la vez una clave privada y una contraseña, o ninguna de las dos.
Dale una sola forma de iniciar sesión: una clave privada, con su passphrase si la
tiene, o una contraseña. "A passphrase goes with a private key." significa que el
login tiene una passphrase y una contraseña. Quita la passphrase.

### SFTP: "The private key could not be read. …" { #sftp-the-private-key-could-not-be-read }

El vault no pudo leer la clave cuando guardaste el login. Pega el archivo de clave
entero, desde su línea `-----BEGIN` hasta su línea `-----END`. Si la clave está
cifrada, da también su passphrase. "The private key could not be read with this
passphrase." significa que la passphrase es incorrecta, o que lo pegado no es una
clave que lea el vault.

La primera línea `-----BEGIN … PRIVATE KEY-----` tiene que ser una de `OPENSSH PRIVATE
KEY`, `PRIVATE KEY`, `ENCRYPTED PRIVATE KEY`, `RSA PRIVATE KEY`, `EC PRIVATE KEY`
o `DSA PRIVATE KEY`. Una clave en forma binaria DER se rechaza, y también una clave
PKCS#8 bajo una cabecera PEM `Proc-Type: 4,ENCRYPTED`. Vuelve a exportar una clave
así en una de estas formas. Para una clave que lee `ssh-keygen`,
`ssh-keygen -p -f <key file>` la escribe en forma OpenSSH. Una clave de PuTTY
(`.ppk`) también se rechaza, y `ssh-keygen` no puede leerla. Expórtala en forma
OpenSSH con PuTTYgen (**Conversions** → **Export OpenSSH key**), o con
`puttygen <key>.ppk -O private-openssh -o <key file>`.

### SFTP: "The private key is encrypted with too many key derivation rounds. …" { #sftp-the-private-key-is-encrypted-with-too-many-key-derivation-rounds }

Una clave cifrada indica cuánto trabajo cuesta comprobar su passphrase. El vault
rechaza una clave que pide más de 128 rondas de bcrypt (OpenSSH), o más de
1.000.000 de iteraciones o una clave derivada de más de 64 bytes (PKCS#8). Lo lee
de la clave, antes de descifrar nada. Vuelve a cifrar la clave con menos rondas,
por ejemplo con `ssh-keygen -p -a 16 -f <key file>`, y vuelve a pegarla.

### SFTP: "The private key takes too long to open. …" { #sftp-the-private-key-takes-too-long-to-open }

La frase completa es "The private key takes too long to open. Use an Ed25519 key,
or an RSA key of at most 4096 bits.". El vault abre cada clave en un proceso
aparte, y lo detiene al cabo de 5 segundos. Una clave cuya parte cifrada pide más
trabajo que eso, o un host de la API muy ocupado, da esta frase. Crea una clave
Ed25519, como se describe en el [paso 2](#2-add-an-ssh-login-to-the-vault), y
vuelve a guardar el login.

### SFTP: "The private key is an RSA key of more than 4096 bits. …" { #sftp-the-private-key-is-an-rsa-key-of-more-than-4096-bits }

La frase completa es "The private key is an RSA key of more than 4096 bits. Use
an Ed25519 key, or an RSA key of at most 4096 bits.". El tiempo de comprobar una
clave RSA crece deprisa con su tamaño, así que el vault rechaza una clave de más
de 4096 bits sin comprobarla. Crea una clave Ed25519, o una clave RSA de 4096 bits
o menos, y añade su mitad pública al `authorized_keys` de la cuenta.

### SFTP: "The private key is an RSA key whose two primes are not of the same size. …" { #sftp-the-private-key-is-an-rsa-key-whose-two-primes-are-not-of-the-same-size }

La frase completa es "The private key is an RSA key whose two primes are not of
the same size. Use an Ed25519 key, or an RSA key that ssh-keygen made.". El vault
rechaza una clave RSA cuyos dos primos difieren en tamaño en más de 16 bits, sin
comprobarla. `ssh-keygen` y OpenSSL crean los dos primos del mismo tamaño, y
comprobar una clave con un primo grande tarda mucho. Crea una clave nueva con
`ssh-keygen`, como se describe en el [paso 2](#2-add-an-ssh-login-to-the-vault), y
añade su mitad pública al `authorized_keys` de la cuenta.

### SFTP: "The private key is held on a security key, which a sync cannot use to log in. …" { #sftp-the-private-key-is-held-on-a-security-key-which-a-sync-cannot-use-to-log-in }

La frase completa es "The private key is held on a security key, which a sync
cannot use to log in. Use an Ed25519 key without a security key.". El archivo de
clave es una clave `sk-`: un identificador de una llave de hardware, como una
creada con `ssh-keygen -t ed25519-sk`. El worker no tiene ninguna llave de
hardware, así que no puede firmar con ella. Crea una clave Ed25519 con
`ssh-keygen -t ed25519`, como se describe en el
[paso 2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "Other private keys are being checked now. Try again in a moment." { #sftp-other-private-keys-are-being-checked-now-try-again-in-a-moment }

Ya se están comprobando dos claves privadas en el mismo proceso de la API, y el
vault comprueba como mucho dos a la vez. El guardado se rechaza en el acto, con el
estado 503 y el código `SERVICE_BUSY`, para que ninguna petición espere mientras
retiene una conexión a la base de datos. Vuelve a guardar el login al cabo de unos
segundos.

### SFTP: "The private key is too long. Paste only the key file, at most 16,384 characters." { #sftp-the-private-key-is-too-long-paste-only-the-key-file-at-most-16384-characters }

El campo **Private key** contiene más de 16.384 caracteres. El archivo de una clave
Ed25519 tiene unos 400 caracteres, y el de una clave RSA de 4096 bits unos 3.300.
Así que lo pegado contiene más que la clave, o no es una clave. Pega solo el
archivo de la clave privada, desde su línea `-----BEGIN` hasta su línea
`-----END`.

### SFTP: "The passphrase is too long: at most 1,024 characters." { #sftp-the-passphrase-is-too-long-at-most-1024-characters }

El campo **Key passphrase** contiene más de 1.024 caracteres. Pega solo la
passphrase que abre la clave. Una passphrase que de verdad es más larga no se
puede guardar: vuelve a cifrar la clave con una más corta, con
`ssh-keygen -p -f <key file>`.

### SFTP: "The password is too long: at most 1,024 characters." { #sftp-the-password-is-too-long-at-most-1024-characters }

El campo **Password** contiene más de 1.024 caracteres. Pega solo la contraseña.
Para un servidor cuya contraseña es más larga, usa una clave en su lugar, como se
describe en el [paso 2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "The private key could not be checked. Try again later." { #sftp-the-private-key-could-not-be-checked-try-again-later }

El proceso que abre la clave se detuvo con un error y no dio ninguna respuesta.
Esto no dice nada de la clave. La API responde con el estado 500. Vuelve a guardar
el login al cabo de un rato. Si vuelve a fallar, el log de la API tiene la línea
`ssh_key_check_failed`. El estado de salida del proceso está en el campo
`returncode` de la entrada de log, que solo muestra un handler de logs que muestre
los campos extra, como Logfire. El host de la API tiene que poder arrancar un
proceso de Python, y ese proceso tiene que poder importar asyncssh.

### SFTP: "The host is a host name or an IP address with an optional port, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-with-an-optional-port }

El **Host** del login no es un único host con un puerto opcional. Escribe un nombre
o una dirección IP, y después `:` y el puerto si no es el 22, como
`sftp.example.com` o `[2001:db8::7]:2222`. Una dirección IPv6 va entre corchetes.
Un esquema, una ruta, un comodín o un espacio se rechazan. Un nombre solo contiene
letras, dígitos, guiones, guiones bajos y puntos.

### SFTP: "This SFTP source has no credential. …" { #sftp-this-sftp-source-has-no-credential }

El `secret_id` de la fuente está vacío, el login que nombraba se ha borrado, o el
worker no pudo abrir el login. El worker comprueba la forma de la clave cada vez
que abre el login, así que una clave guardada que ya no pasa esa comprobación
también da esta frase. Entonces el log del worker tiene
`sync_source_secret_unusable`. Añade un login SSH al vault, como se describe en el
[paso 2](#2-add-an-ssh-login-to-the-vault), o vuelve a guardar la clave y su
passphrase en el login. Después elígelo en el paso de credencial de la fuente.

### SFTP: "An SFTP source needs an SSH login, and the credential it names is not one." { #sftp-an-sftp-source-needs-an-ssh-login-and-the-credential-it-names-is-not-one }

Una fuente que nombra una credencial de otro kind, como una API key, se rechaza al
guardarla, con "A SFTP server source needs a ssh_credentials credential, and that
one is …". Esto vale para el asistente, la API y `rag-source-add`. Elige un **SSH
login**. La frase del título es la comprobación propia del connector al
sincronizar, y una fuente guardada con un login SSH no la da.

### SFTP: "This SSH login was added for …, and the source names …" { #sftp-this-ssh-login-was-added-for-and-the-source-names }

El host o el puerto de la fuente no son aquellos con los que se añadió el login. O
**Host** o **Port** son incorrectos, o la fuente necesita un login añadido para ese
host. Un login añadido sin puerto solo se usa en el puerto 22. No se envió nada al
host de la fuente.

### SFTP: "… did not prove that it holds the host key in this source's settings. …" { #sftp-did-not-prove-that-it-holds-the-host-key-in-this-sources-settings }

El servidor respondió con una clave de host distinta de la de **Host key**. La
clave del servidor cambió, por ejemplo porque el servidor se volvió a instalar, o
el worker no llegó al servidor real. Vuelve a comparar la huella con la del
administrador, como se describe en el [paso 1](#1-get-the-servers-host-key),
antes de cambiar la clave. No se envió ningún login.

### SFTP: "… and this worker could not agree on a host key or a cipher. …" { #sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher }

En una primera sincronización, comprueba primero el tipo de clave de host. El
worker solo pide al servidor una clave del tipo que hay en **Host key**, y Apache
MINA SSHD responde así cuando no tiene ninguna clave de ese tipo. Obtén la clave
del servidor de otro tipo, como se describe en el
[paso 1](#1-get-the-servers-host-key). Si el tipo es correcto, el servidor no
acepta ninguno de los cifrados que ofrece el worker. Pregunta al administrador
cuáles permite. Un servidor que solo ofrece conexiones comprimidas también da esta
frase, porque el worker nunca activa la compresión.

### SFTP: "The connection to … was lost." { #sftp-the-connection-to-was-lost }

En una primera sincronización, comprueba primero el tipo de clave de host. OpenSSH
y SFTPGo terminan la conexión así cuando no tienen ninguna clave del tipo que hay
en **Host key**.

OpenSSH 9.8 y posteriores también rechazan todas las conexiones desde una dirección
que falló varias veces al iniciar sesión (`PerSourcePenalties`). Todas las fuentes
de un deployment suelen conectarse desde una sola dirección, la de los workers. Así
que una fuente con una contraseña errónea puede hacer que, durante un tiempo, las
sincronizaciones de otras fuentes del mismo servidor fallen con esta frase.

Busca una fuente del mismo host cuya última sincronización dio "The login to … did
not complete.". Corrige su login, o páusala. Después vuelve a sincronizar cuando
haya terminado la penalización: por defecto crece hasta 10 minutos como mucho. El
log del servidor muestra la penalización como `srclimit_penalise`. El
administrador del servidor también puede eximir la dirección de los workers con
`PerSourcePenaltyExemptList`. Eso quita la penalización a las fuentes de todas las
organizaciones del deployment, así que corrige primero el login.

Si no, la red o el servidor terminaron la conexión, la reiniciaron o terminaron la
sesión SFTP, o el servidor no respondió a tres keepalives. Un listado que pierde
la conexión hace fallar la sincronización entera, y ese run no elimina nada. Una
descarga que la pierde hace fallar ese archivo, y el siguiente archivo abre una
conexión nueva.

### SFTP: "… closed the connection." { #sftp-closed-the-connection }

En una primera sincronización, comprueba primero el tipo de clave de host. ProFTPD
termina la conexión así cuando no tiene ninguna clave del tipo que hay en **Host
key**. Si no, el servidor terminó la conexión mientras se abría, por ejemplo
porque no admite más sesiones o rechaza la dirección del worker. El log del worker
tiene el motivo del propio servidor.

### SFTP: "The login to … as … did not complete. …" { #sftp-the-login-to-as-did-not-complete }

El servidor rechazó el nombre de usuario, la clave o la contraseña. Compruébalos
en el vault. Para una clave, comprueba que el administrador añadió su mitad
pública al `authorized_keys` de la cuenta. Un servidor que solo acepta una
contraseña mediante un login keyboard-interactive rechaza un login con contraseña,
así que ahí usa una clave. Un servidor también puede rechazar el tipo de la clave.
Apache MINA SSHD sin su biblioteca EdDSA rechaza una clave Ed25519, así que ahí
prueba una clave ECDSA. Un servidor que deja entrar al worker sin login también se
rechaza con esta frase.

La sincronización conserva este rechazo, así que el servidor recibe un login
fallido, no uno por archivo. Cada sincronización posterior envía uno más. En
OpenSSH 9.8 y posteriores, unos pocos hacen que el servidor rechace durante un
tiempo la dirección de los workers, y las demás fuentes de ese servidor fallan
entonces con [se perdió la conexión](#sftp-the-connection-to-was-lost). Corrige el
login, o pausa la fuente.

### SFTP: "The private key in the Vault could not be read with its passphrase." { #sftp-the-private-key-in-the-vault-could-not-be-read-with-its-passphrase }

El worker no pudo abrir la clave privada del login con la passphrase guardada
junto a ella. El vault comprueba la clave cuando se guarda el login, así que esto
es raro. Vuelve a guardar la clave y su passphrase en el login.

### SFTP: "… has no SFTP service for …" { #sftp-has-no-sftp-service-for }

El servidor aceptó el login, pero no da a la cuenta ningún servicio SFTP. La
cuenta puede ser solo para un shell, o el servidor no ejecuta ningún subsistema
SFTP. Pide al administrador que permita SFTP para la cuenta.

### SFTP: "… answered the start of the SFTP session in a way this worker cannot use." { #sftp-answered-the-start-of-the-sftp-session-in-a-way-this-worker-cannot-use }

El servidor respondió a la petición de versión de SFTP de una forma que el worker
no puede usar, o con una respuesta que el worker no puede descodificar. El worker
habla la versión 3 de SFTP, como OpenSSH, SFTPGo, ProFTPD
y Apache MINA SSHD. El log del worker tiene la respuesta del propio servidor.
Pregunta al administrador por la configuración SFTP del servidor.

### SFTP: "… did not answer within 30 seconds." { #sftp-did-not-answer-within-30-seconds }

El servidor no respondió a la conexión, o a una petición SFTP, en 30 segundos. Al
conectar, la causa habitual es un firewall que descarta los paquetes del worker.
Comprueba que el worker puede llegar al puerto SSH del servidor. Durante la
sincronización, el servidor dejó de responder. El worker corta la conexión, y el
listado o ese archivo fallan. El listado lee varias carpetas a la vez, así que una
carpeta que se atasca hace fallar el listado entero con esta frase, y ese run no
elimina nada.

### SFTP: "… could not be reached on port …" { #sftp-could-not-be-reached-on-port }

El servidor rechazó la conexión en ese puerto, o la red no tiene ruta hasta él.
Comprueba **Port**, y comprueba que un firewall deja que el worker llegue al
servidor. Cuando el host tiene más de una dirección, el worker prueba cada una
antes de rendirse.

### SFTP: "The sync did not finish (OSError) - check the source's credential, …" { #sftp-the-sync-did-not-finish-oserror-check-the-sources-credential }

En una fuente SFTP, la credencial no suele ser la causa. Esta frase suele significar
que el worker se ejecuta como un usuario sin entrada en passwd, así que la
biblioteca SSH no puede leer el nombre de usuario local del worker. Configura
`LOGNAME` para el worker, como se describe en la nota "Un worker necesita un nombre
de usuario local" en [Reglas de red de SFTP](#sftp-network-rules). El log del
worker tiene el error completo.

### SFTP: "There is no folder … on …, or it is not a folder." { #sftp-there-is-no-folder-on-or-it-is-not-a-folder }

**Folder** no nombra ninguna carpeta que la cuenta pueda ver. Comprueba la ruta.
En un servidor que mantiene la cuenta en una carpeta personal o en un chroot, la
ruta empieza en esa carpeta, no en la raíz del servidor. OpenSSH da la misma
respuesta cuando **Folder** nombra un archivo. Otros servidores dan "… could not
list the folder …" para un archivo.

### SFTP: "The account … may not read the folder … on …" { #sftp-the-account-may-not-read-the-folder-on }

La cuenta no tiene permiso para listar **Folder**. Pide al administrador que dé a
la cuenta acceso de lectura a la carpeta, o elige una carpeta que la cuenta pueda
leer.

### SFTP: "… could not list the folder …" { #sftp-could-not-list-the-folder }

La frase completa es "… could not list the folder …. Check that it is a folder
the account may read.". SFTPGo, ProFTPD y Apache MINA SSHD responden así cuando
**Folder** nombra un archivo, así que comprueba primero la ruta. Si no, el
servidor no pudo listar la carpeta por otro motivo, o envió una respuesta que el
worker no puede descodificar. El log del worker tiene la respuesta del propio
servidor.

### SFTP: "The listing of … stopped …. Choose a narrower folder." { #sftp-the-listing-of-stopped-choose-a-narrower-folder }

El listado alcanzó uno de [los límites](#what-bounds-an-sftp-sync), y la frase
nombra cuál: 32 niveles de carpeta, 10000 carpetas, 1000000 nombres, 100000
archivos, 64 MB de rutas de archivo o 1800 segundos. Lo que encontró el listado se ingirió, y no se eliminó
nada. Un límite no se puede subir. Elige una carpeta más estrecha en **Folder**, o
divide el árbol en varias fuentes, hasta que una sincronización termine sin esta
frase.

### SFTP: "The folder … could not be read." { #sftp-the-folder-could-not-be-read }

La carpeta padre lista esta carpeta, pero el servidor se negó a abrirla,
normalmente porque la cuenta no tiene permiso, o envió sobre ella una respuesta
que el worker no puede descodificar. Las demás carpetas se ingirieron,
y en ese run no se eliminó nada. Da a la cuenta acceso de lectura a la carpeta, o
saca la carpeta del árbol.

### SFTP: nombres que quedan fuera del listado { #sftp-names-left-out-of-the-listing }

El registro de sincronización puede dar estas frases para una carpeta:

- "The server listed … names in … that are not file names, so they were left
  out." Los nombres están vacíos, contienen `/` o un byte NUL, u ocupan más de
  255 bytes, o sus rutas ocupan más de 4.096 bytes.
- "… names in … are not UTF-8, so those files were left out."
- "The server did not say what … entries in … are, so they were left out."

En los dos primeros casos, renombra los archivos en el servidor. El listado sigue
completo. En el tercero, el servidor no dio ningún tipo para algunas entradas. El
listado queda entonces incompleto, así que ese run no elimina nada. Pregunta al
administrador por el servidor. El registro de sincronización muestra como mucho
tres notas. Corrige las carpetas que nombra y vuelve a sincronizar para ver las
siguientes. Un carácter de control en el nombre de una carpeta, o un separador
Unicode de línea o de párrafo, se muestra como `?`.

### SFTP: un archivo contado como fallido { #sftp-a-file-counted-as-failed }

El registro de sincronización cuenta el archivo en `failed`, y los demás archivos
se sincronizan. El log del worker nombra el archivo y el motivo, en una línea como
`Failed to sync report.pdf: report.pdf is 61 MB, and a synced file may be at most 50 MB.`

| Motivo | Causa | Qué hacer |
|---|---|---|
| "… is … MB, and a synced file may be at most … MB." | El archivo supera `MAX_UPLOAD_SIZE_MB`. Falla en cada sincronización, antes de leer un solo byte | Saca el archivo de la carpeta, quita su tipo de **File types** o sube `MAX_UPLOAD_SIZE_MB` |
| "The server sent more than … MB of …, and a synced file may be at most … MB." | El servidor envió más que el límite, aunque sus atributos daban un tamaño menor | Pregunta al administrador por el archivo |
| "The account … may not read …" | Los permisos del archivo rechazan a la cuenta | Da a la cuenta acceso de lectura al archivo |
| "The server refused to read …" | El servidor rechazó el archivo por otro motivo, o envió una respuesta que el worker no puede descodificar. El log del worker tiene su respuesta | Pregunta al administrador |
| "The server listed … as a file, and now says it is not one." | La ruta es ahora una carpeta, un enlace o un archivo especial, o las respuestas del servidor no coinciden | El siguiente listado lo resuelve. Si falla en cada sincronización, pregunta al administrador |
| "The server lists …, but answers that it does not exist." | La carpeta sigue nombrando el archivo, pero el servidor responde "no such file" para él | Pregunta al administrador. El servidor se contradice |
| "The server answers that … does not exist, and its folder could not be listed to confirm it." | El servidor respondió "no such file", y después no pudo listar la carpeta del archivo, o no dentro de los 10 minutos del archivo | Comprueba los permisos de la carpeta. La siguiente sincronización lo vuelve a intentar |
| "The server sent … of the … bytes … holds." | El servidor envió menos bytes de los que dan sus atributos | La siguiente sincronización lo vuelve a intentar. Si vuelve a fallar, pregunta al administrador |
| "The server answered a read of … with more than was asked for." | El servidor envió más bytes de los que pedía una petición | Pregunta al administrador. El servidor está roto |
| "The server opened a file for … that is not the file it listed. …" | El archivo abierto tiene otro propietario o grupo que el listado, u otros permisos que el stat de antes de la apertura o, para un archivo cuyo tamaño u hora cambió, que el listado | Consulta [su entrada](#sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed) |
| "… did not download within 600 seconds." | La descarga tardó más de 10 minutos. Un archivo de 50 MB necesita unos 85 KB/s | Comprueba la velocidad de la red hasta el servidor |
| "… did not answer within 30 seconds." o "The connection to … was lost." | El servidor dejó de responder, o la conexión terminó durante la descarga | El siguiente archivo abre una conexión nueva, y la siguiente sincronización lee este archivo |

Cuando falla una conexión nueva durante la sincronización, la sincronización
conserva ese rechazo. Cada archivo posterior falla con la misma frase. Consulta
más arriba las entradas sobre la conexión.

### SFTP: "The server opened a file for … that is not the file it listed. …" { #sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed }

La frase completa es "The server opened a file for … that is not the file it
listed. Check that nobody can replace files in this folder with links.". El
archivo que abrió el servidor tiene otro propietario o grupo que los que dio el
listado, u otros permisos que los del stat justo antes de la apertura. Cuando ese
stat da otro tamaño u otra hora de modificación que el listado, los permisos
también tienen que ser los del listado. La causa habitual es un enlace puesto en
lugar del archivo listado, o de una carpeta superior, que puede hacer que la
sincronización lea otro archivo.

El archivo no se ingiere. Consulta [el aviso sobre los
enlaces](#4-what-an-sftp-sync-transfers), y encierra la cuenta en el servidor. Un
archivo que otra cuenta volvió a subir entre el listado y la descarga da esta frase
una vez, y la siguiente sincronización lo lee. Lo mismo ocurre con un archivo que
cambió en un servidor que falsea los permisos de su listado, como ProFTPD con
`DirFakeMode`.

### SFTP: "… files changed or were deleted while this sync ran, …" { #sftp-files-changed-or-were-deleted-while-this-sync-ran }

Estos archivos cambiaron durante la lectura, o se borraron después del listado. Se
cuentan como `skipped`, y la siguiente sincronización los lee o elimina sus
documentos. El connector registra una de estas frases para cada archivo, en el
nivel info: "… changed while it was downloaded.", "… was replaced while it was
downloaded.", o "… was deleted before it was downloaded." (o "while it was
downloaded."). El worker que se distribuye no imprime el nivel info, así que su
log no las muestra por defecto. Si un archivo muestra esto en cada sincronización,
una causa es un servidor que da la hora actual como hora de modificación. Pregunta
al administrador por el servidor.

### Las sincronizaciones programadas no se ejecutan { #scheduled-syncs-are-not-running }

Tiene que haber un sistema de tareas en segundo plano en marcha. Comprueba que
tu proceso worker está activo:

Sin un worker, solo funcionan los disparos manuales desde la CLI o la API.
