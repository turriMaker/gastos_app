# Bot de Gastos — Francisco & Anna

## Variables de entorno (configurar en Railway)

| Variable | Descripción |
|---|---|
| TELEGRAM_TOKEN | Token del bot de @BotFather |
| SUPABASE_URL | URL del proyecto Supabase |
| SUPABASE_KEY | anon public key de Supabase |
| ANTHROPIC_KEY | API key de Anthropic |

## Deploy en Railway

1. Subir esta carpeta a un repositorio GitHub
2. Crear nuevo proyecto en railway.app → Deploy from GitHub
3. Agregar las 4 variables de entorno en Settings → Variables
4. Railway detecta el Procfile y corre el bot automáticamente

## Uso

Mensajes en lenguaje natural:
- "pagué 4500 de nafta" → gasto individual
- "anna pagó 12000 de almacén compartido" → gasto compartido
- "balance" → ver deuda actual
- "resumen de abril" → resumen del mes
- "saldar 5000" → registrar transferencia

Comandos:
- /ver_fijos → lista de gastos predefinidos
- /nuevo_fijo cole,bondi | Colectivo | 1790 | individual | francisco | transporte
- /borrar_fijo cole
