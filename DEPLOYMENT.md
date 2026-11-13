# Despliegue en Render con Supabase

La aplicación se ejecutará en Render y usará PostgreSQL y un bucket privado de
Supabase Storage. La base SQLite y las facturas locales no se migrarán: la
producción empezará vacía.

## 1. Preparar Supabase

1. Abre el proyecto de Supabase y entra en **Storage**.
2. Crea un bucket llamado `invoices` y deja desactivada la opción **Public**
   para que sea privado.
3. En **Project Settings**, copia la URL del proyecto y crea o copia una clave
   secreta de servidor (**Secret key**). No uses una clave pública/publishable
   ni la clave `anon`. Mantén la clave privada.
4. Obtén la cadena de conexión de PostgreSQL. En la ventana **Conéctate a tu
   proyecto**, pulsa **Directo — Cadena de conexión** (no **Servidor — Crear
   API**). En la pantalla siguiente, selecciona el modo **Session pooler** y
   copia la URI de PostgreSQL. Si la pantalla muestra opciones como `URI`,
   selecciona ese formato. La cadena incluye el usuario, host, puerto, base de
   datos y una contraseña; mantenla privada.

La conexión de Supabase con GitHub puede servir para integrar el proyecto de
Supabase con un repositorio y administrar cambios de esquema, pero no entrega
las credenciales de la base de datos o Storage a la aplicación ni las configura
automáticamente en Render.

## 2. Publicar la aplicación en Render

1. Asegúrate de que los cambios de la aplicación estén en el repositorio de
   GitHub. En Render, crea un **Blueprint** conectado a ese repositorio;
   Render leerá la configuración de `render.yaml`.
2. En la configuración del servicio de Render, agrega estos valores como
   variables de entorno privadas:
   - `DATABASE_URL`: la cadena de conexión de PostgreSQL de Supabase.
   - `SUPABASE_URL`: la URL del proyecto de Supabase.
   - `SUPABASE_SECRET_KEY`: la clave secreta de servidor de Supabase.
   - `SECRET_KEY`: un secreto nuevo para las sesiones de Flask.
3. Genera un `SECRET_KEY` único en tu computadora con PowerShell:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Pega el resultado directamente en la variable privada `SECRET_KEY` de Render.
   No lo subas a GitHub ni lo envíes por chat. `APP_ENV` y el nombre del bucket
   `invoices` ya están definidos en `render.yaml`.
4. Inicia el despliegue. Cuando termine, abre la dirección del servicio seguida
   de `/health`; debe responder `{"status":"ok"}`.

En el primer inicio, la aplicación crea las tablas que necesita en la base de
datos PostgreSQL vacía. Las facturas se guardan en el bucket privado y se
entregan a través de una ruta que requiere iniciar sesión.

## 3. Conectar el dominio de Vercel

Cuando el servicio de Render esté activo:

1. Agrega tu dominio en la configuración del servicio de Render.
2. Render mostrará los registros DNS específicos que debes crear.
3. En la configuración DNS del dominio en Vercel, agrega esos registros tal
   como los muestra Render. No uses valores de ejemplo: dependen del servicio.

## Datos locales y archivos del repositorio

`database.db` y los archivos de `uploads/invoices/` permanecen en tu
computadora y están excluidos de futuros commits. Este despliegue no los importa
a Supabase. Quitarlos de la versión actual del repositorio no borra las copias
que pudieran existir en commits anteriores. Si contienen información sensible,
revisa el acceso y el historial del repositorio antes de hacerlo público.
