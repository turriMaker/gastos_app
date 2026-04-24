import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes, CallbackContext
from groq import Groq
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GROQ_KEY = os.environ["GROQ_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_KEY)

USUARIOS = {"francisco", "anna"}
CATEGORIAS = ["comida", "transporte", "servicios", "salud", "ocio", "hogar", "ropa", "educacion", "otros"]

SYSTEM_PROMPT = """Sos un asistente que parsea mensajes de gastos para una pareja (Francisco y Anna).
Dado un mensaje, devolvés SOLO un objeto JSON con estos campos:
- accion: "gasto" | "saldar" | "balance" | "resumen" | "ver_fijos" | "nuevo_fijo" | "borrar_fijo" | "desconocido"
- descripcion: string (solo si accion=gasto)
- monto: number (solo si accion=gasto o saldar)
- pagador: "francisco" | "anna" | null
- tipo: "individual" | "compartido" | null
- categoria: una de [comida, transporte, servicios, salud, ocio, hogar, ropa, educacion, otros] | null
- confianza: "alta" | "media" | "baja"
- de: "francisco" | "anna" | null (solo si accion=saldar, quien paga)
- hacia: "francisco" | "anna" | null (solo si accion=saldar, quien recibe)
- periodo: string | null (solo si accion=resumen, ej: "abril", "este mes")
- alias_fijo: string | null (para nuevo_fijo o borrar_fijo)
- nota: string | null

Reglas:
- Si el mensaje es ambiguo sobre quien pagó, inferilo del contexto o dejá null con confianza baja
- Si dice "pagué" o "pague" sin nombre, el pagador es quien escribe (se resuelve externamente)
- Gastos de comida/super/verduras/almacen → categoria comida
- Nafta/colectivo/uber/taxi/peaje → transporte
- Luz/gas/agua/internet/alquiler → servicios
- Medico/farmacia/remedios → salud
- Cine/restaurante/bar/salida → ocio
- Limpieza/muebles/reparacion → hogar
- Solo devolvé el JSON, sin texto adicional"""

def get_user_name(update: Update) -> str:
    """Infiere el nombre del usuario desde Telegram."""
    first = (update.effective_user.first_name or "").lower()
    if "francisco" in first or "fran" in first:
        return "francisco"
    if "anna" in first or "ana" in first:
        return "anna"
    return update.effective_user.first_name.lower()

def calcular_balance_impacto(tipo: str, pagador: str, monto: float) -> float:
    """
    Retorna el impacto en el balance desde la perspectiva de 'anna le debe a francisco'.
    Positivo = anna debe más. Negativo = francisco debe más.
    """
    if tipo != "compartido":
        return 0.0
    mitad = round(monto / 2, 2)
    if pagador == "francisco":
        return mitad   # francisco pagó la parte de anna
    elif pagador == "anna":
        return -mitad  # anna pagó la parte de francisco
    return 0.0

async def parsear_mensaje(texto: str, usuario: str) -> dict:
    """Llama a Groq para parsear el mensaje."""
    prompt = f"Usuario que escribe: {usuario}\nMensaje: {texto}"
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=500,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

async def chequear_predefinido(texto: str) -> dict | None:
    """Busca si el mensaje coincide con algún alias predefinido."""
    result = supabase.table("predefinidos").select("*").eq("eliminado", False).execute()
    texto_lower = texto.strip().lower()
    for item in result.data:
        if texto_lower in [a.lower() for a in item["aliases"]]:
            return item
    return None

async def handle_gasto(update: Update, parsed: dict, usuario: str, raw: str, origen: str = "libre"):
    monto = parsed.get("monto")
    descripcion = parsed.get("descripcion", "Sin descripción")
    pagador = parsed.get("pagador") or usuario
    tipo = parsed.get("tipo", "individual")
    categoria = parsed.get("categoria", "otros")
    confianza = parsed.get("confianza", "alta")

    if not monto:
        await update.message.reply_text("No pude entender el monto. ¿Podés ser más específico?")
        return

    balance_impacto = calcular_balance_impacto(tipo, pagador, monto)

    data = {
        "descripcion": descripcion,
        "monto": monto,
        "pagador": pagador,
        "tipo": tipo,
        "categoria": categoria,
        "balance_impacto": balance_impacto,
        "confianza": confianza,
        "origen": origen,
        "raw_message": raw,
        "eliminado": False
    }
    supabase.table("gastos").insert(data).execute()

    tipo_emoji = "🤝" if tipo == "compartido" else "👤"
    confianza_aviso = " _(no estaba seguro, revisá)_" if confianza == "baja" else ""
    msg = (
        f"✅ *{descripcion}* — ${monto:,.0f}\n"
        f"💳 Pagó: {pagador.capitalize()}\n"
        f"{tipo_emoji} {tipo.capitalize()} · {categoria.capitalize()}"
        f"{confianza_aviso}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_saldar(update: Update, parsed: dict, usuario: str):
    monto = parsed.get("monto")
    de = parsed.get("de") or usuario
    hacia_opciones = USUARIOS - {de}
    hacia = parsed.get("hacia") or list(hacia_opciones)[0]

    if not monto:
        await update.message.reply_text("No pude entender el monto a saldar.")
        return

    supabase.table("saldos").insert({
        "monto": monto,
        "de": de,
        "hacia": hacia,
        "nota": parsed.get("nota"),
        "eliminado": False
    }).execute()

    await update.message.reply_text(
        f"✅ Saldo registrado: {de.capitalize()} → {hacia.capitalize()} *${monto:,.0f}*",
        parse_mode="Markdown"
    )

async def handle_balance(update: Update):
    result = supabase.rpc("balance", {}).execute() if False else None
    
    gastos = supabase.table("gastos").select("tipo,pagador,monto").eq("eliminado", False).execute()
    saldos = supabase.table("saldos").select("de,hacia,monto").eq("eliminado", False).execute()

    balance = 0.0
    for g in gastos.data:
        balance += calcular_balance_impacto(g["tipo"], g["pagador"], g["monto"])
    for s in saldos.data:
        if s["de"] == "anna" and s["hacia"] == "francisco":
            balance -= s["monto"]
        elif s["de"] == "francisco" and s["hacia"] == "anna":
            balance += s["monto"]

    if abs(balance) < 0.5:
        msg = "⚖️ *Balance al día* — no hay deudas pendientes."
    elif balance > 0:
        msg = f"📊 *Balance actual*\nAnna le debe a Francisco *${balance:,.0f}*"
    else:
        msg = f"📊 *Balance actual*\nFrancisco le debe a Anna *${abs(balance):,.0f}*"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_resumen(update: Update, periodo: str | None):
    gastos = supabase.table("gastos").select("*").eq("eliminado", False).order("fecha", desc=True).execute()
    data = gastos.data

    if periodo:
        meses = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
        }
        mes_num = next((v for k, v in meses.items() if k in periodo.lower()), None)
        if mes_num:
            data = [g for g in data if datetime.fromisoformat(g["fecha"]).month == mes_num]

    if not data:
        await update.message.reply_text("No hay gastos registrados para ese período.")
        return

    total = sum(g["monto"] for g in data)
    compartidos = sum(g["monto"] for g in data if g["tipo"] == "compartido")
    individuales = sum(g["monto"] for g in data if g["tipo"] == "individual")

    por_categoria = {}
    for g in data:
        por_categoria[g["categoria"]] = por_categoria.get(g["categoria"], 0) + g["monto"]

    cat_texto = "\n".join(
        f"  · {k.capitalize()}: ${v:,.0f}"
        for k, v in sorted(por_categoria.items(), key=lambda x: -x[1])
    )

    msg = (
        f"📋 *Resumen{' ' + periodo if periodo else ''}*\n\n"
        f"Total: *${total:,.0f}*\n"
        f"🤝 Compartidos: ${compartidos:,.0f}\n"
        f"👤 Individuales: ${individuales:,.0f}\n\n"
        f"*Por categoría:*\n{cat_texto}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_ver_fijos(update: Update):
    result = supabase.table("predefinidos").select("*").eq("eliminado", False).execute()
    if not result.data:
        await update.message.reply_text("No hay gastos fijos configurados.")
        return
    lineas = []
    for p in result.data:
        aliases = ", ".join(p["aliases"])
        pagador = p["pagador"] or "quien escribe"
        lineas.append(f"• *{aliases}* → {p['descripcion']} ${p['monto']:,.0f} ({p['tipo']}, {pagador})")
    await update.message.reply_text("📌 *Gastos fijos:*\n" + "\n".join(lineas), parse_mode="Markdown")

async def handle_nuevo_fijo(update: Update, texto: str):
    await update.message.reply_text(
        "Para crear un gasto fijo usá el formato:\n"
        "`/nuevo_fijo alias1,alias2 | descripcion | monto | tipo | pagador | categoria`\n\n"
        "Ejemplo:\n`/nuevo_fijo cole,bondi | Colectivo | 1790 | individual | francisco | transporte`",
        parse_mode="Markdown"
    )

async def cmd_nuevo_fijo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args)
    partes = [p.strip() for p in args.split("|")]
    if len(partes) < 5:
        await update.message.reply_text(
            "Formato: `/nuevo_fijo alias1,alias2 | descripcion | monto | tipo | pagador | categoria`\n"
            "Ejemplo: `/nuevo_fijo cole,bondi | Colectivo | 1790 | individual | francisco | transporte`",
            parse_mode="Markdown"
        )
        return

    aliases = [a.strip().lower() for a in partes[0].split(",")]
    descripcion = partes[1]
    monto = float(partes[2].replace(".", "").replace(",", "."))
    tipo = partes[3].lower()
    pagador = partes[4].lower() if partes[4].lower() in USUARIOS else None
    categoria = partes[5].lower() if len(partes) > 5 else "otros"

    supabase.table("predefinidos").insert({
        "aliases": aliases,
        "descripcion": descripcion,
        "monto": monto,
        "tipo": tipo,
        "pagador": pagador,
        "categoria": categoria,
        "eliminado": False
    }).execute()

    await update.message.reply_text(f"✅ Gasto fijo *{descripcion}* creado con aliases: {', '.join(aliases)}", parse_mode="Markdown")

async def cmd_borrar_fijo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usá `/borrar_fijo alias`", parse_mode="Markdown")
        return
    alias = context.args[0].lower()
    result = supabase.table("predefinidos").select("*").eq("eliminado", False).execute()
    encontrado = next((p for p in result.data if alias in [a.lower() for a in p["aliases"]]), None)
    if not encontrado:
        await update.message.reply_text(f"No encontré ningún fijo con el alias '{alias}'.")
        return
    supabase.table("predefinidos").update({"eliminado": True}).eq("id", encontrado["id"]).execute()
    await update.message.reply_text(f"🗑️ Gasto fijo *{encontrado['descripcion']}* eliminado.", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    usuario = get_user_name(update)

    predefinido = await chequear_predefinido(texto)
    if predefinido:
        pagador = predefinido.get("pagador") or usuario
        fake_parsed = {
            "monto": predefinido["monto"],
            "descripcion": predefinido["descripcion"],
            "pagador": pagador,
            "tipo": predefinido["tipo"],
            "categoria": predefinido["categoria"],
            "confianza": "alta"
        }
        await handle_gasto(update, fake_parsed, usuario, texto, origen="predefinido")
        return

    try:
        parsed = await parsear_mensaje(texto, usuario)
    except Exception as e:
        logger.error(f"Error parseando: {e}")
        await update.message.reply_text("No pude entender el mensaje. ¿Podés reformularlo?")
        return

    accion = parsed.get("accion", "desconocido")

    if accion == "gasto":
        await handle_gasto(update, parsed, usuario, texto)
    elif accion == "saldar":
        await handle_saldar(update, parsed, usuario)
    elif accion == "balance":
        await handle_balance(update)
    elif accion == "resumen":
        await handle_resumen(update, parsed.get("periodo"))
    elif accion == "ver_fijos":
        await handle_ver_fijos(update)
    elif accion in ("nuevo_fijo", "borrar_fijo"):
        await handle_nuevo_fijo(update, texto)
    else:
        await update.message.reply_text(
            "No entendí. Podés decirme cosas como:\n"
            "• _pagué 3500 de verduras_\n"
            "• _anna pagó 8000 de luz compartido_\n"
            "• _balance_\n"
            "• _resumen de abril_\n"
            "• _saldar 5000_\n"
            "• _ver fijos_",
            parse_mode="Markdown"
        )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Bot de gastos de Francisco y Anna*\n\n"
        "Mandame un gasto en lenguaje natural y lo registro automáticamente.\n\n"
        "Ejemplos:\n"
        "• _pagué 4500 de nafta_\n"
        "• _anna pagó 12000 de almacén compartido_\n"
        "• _balance_\n"
        "• _resumen de abril_\n"
        "• _saldar 3000_\n\n"
        "Comandos:\n"
        "/ver\\_fijos — ver gastos predefinidos\n"
        "/nuevo\\_fijo — crear gasto predefinido\n"
        "/borrar\\_fijo — eliminar gasto predefinido",
        parse_mode="Markdown"
    )

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_balance(update)

async def cmd_ver_fijos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_ver_fijos(update)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("ver_fijos", cmd_ver_fijos))
    app.add_handler(CommandHandler("nuevo_fijo", cmd_nuevo_fijo))
    app.add_handler(CommandHandler("borrar_fijo", cmd_borrar_fijo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
