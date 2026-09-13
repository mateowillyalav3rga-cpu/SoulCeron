import os
import re
import io
import logging
import asyncio
import psycopg2
from datetime import datetime
from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHAT_ID_ADMIN = os.getenv("CHAT_ID_ADMIN")

web_app = Flask(__name__)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def enviar_mensaje_seguro(texto: str, max_length: int = 4000) -> str:
    if len(texto) > max_length:
        return texto[:max_length] + "\n\n⚠️ *(Resultado recortado por longitud)*"
    return texto

def formatear_cop(monto):
    return f"${monto:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")

# -------------------------------------------------------------------
# MENÚ CON BOTONES INTERACTIVOS (BÚSQUEDA MANUAL)
# -------------------------------------------------------------------
def obtener_teclado_menu():
    keyboard = [
        [
            InlineKeyboardButton("📊 Ventas Hoy (Al momento)", callback_data="btn_ventas_hoy"),
            InlineKeyboardButton("📅 Lo que va de Semana", callback_data="btn_ventas_semana")
        ],
        [
            InlineKeyboardButton("🏢 Valorización Bodega", callback_data="btn_valorizacion"),
            InlineKeyboardButton("⚠️ Stock Bajo", callback_data="btn_stock_bajo")
        ],
        [
            InlineKeyboardButton("📄 Reporte PDF Mes", callback_data="btn_reporte_pdf"),
            InlineKeyboardButton("❓ Ayuda / Formatos", callback_data="btn_ayuda")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✨ **¡Bienvenido al Bot de Gestión de Soulcerón!** ✨\n\n"
        "Consulta en tiempo real con los botones o registra ventas y compras enviando sus comandos."
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🤖 **Formatos de Registro y Consultas Manuales - Soulcerón**\n\n"
        "📝 **Registrar Venta:**\n"
        "```\n"
        "/venta\n"
        "Cliente: Nombre Apellido\n"
        "Pago: contado\n"
        "- Pomo, 1\n"
        "```\n\n"
        "📦 **Registrar Compra / Reabastecimiento:**\n"
        "```\n"
        "/compra\n"
        "- Pomo, 50, 1300, 3000\n"
        "```\n\n"
        "🔍 **Consultas Manuales por Comando:**\n"
        "• `/ventas_hoy` : Lo acumulado el día de hoy.\n"
        "• `/ventas_semana` : Lo acumulado en la semana en curso.\n"
        "• `/cliente Nombre` : Historial y desglose de un cliente."
    )
    if update.message:
        await update.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

# -------------------------------------------------------------------
# LÓGICA DE CONSULTAS SQL
# -------------------------------------------------------------------
async def ventas_hoy_logic():
    query = """
    SELECT COUNT(DISTINCT v.id_venta) AS total_ventas, COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE(v.fecha_venta) = CURRENT_DATE;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0], res[1]

async def ventas_semana_logic():
    query = """
    SELECT COUNT(DISTINCT v.id_venta) AS total_ventas, COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('week', v.fecha_venta) = DATE_TRUNC('week', CURRENT_DATE);
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0], res[1]

async def stock_bajo_logic():
    query = """
    SELECT nombre, stock_actual 
    FROM productos 
    WHERE stock_actual <= 5 
    ORDER BY stock_actual ASC
    LIMIT 20;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas

async def valorizacion_logic():
    query = """
    SELECT SUM(stock_actual * costo_compra) AS costo_total, SUM(stock_actual * precio_venta) AS valor_venta_total
    FROM productos;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0] or 0, res[1] or 0

# -------------------------------------------------------------------
# REGISTRO DE VENTAS Y COMPRAS
# -------------------------------------------------------------------
async def registrar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text

    cliente_match = re.search(r"Cliente:\s*(.+)", texto, re.IGNORECASE)
    pago_match = re.search(r"Pago:\s*(.+)", texto, re.IGNORECASE)
    productos_matches = re.findall(r"-\s*(.+?),\s*(\d+)", texto)

    if not cliente_match or not pago_match or not productos_matches:
        await update.message.reply_text("❌ **Formato incorrecto.** Usa:\n/venta\nCliente: Nombre\nPago: contado\n- Producto, Cantidad")
        return

    cliente_nombre = cliente_match.group(1).strip()
    tipo_pago = pago_match.group(1).strip().lower()

    if tipo_pago not in ['contado', 'credito']:
        await update.message.reply_text("❌ El tipo de pago debe ser exclusivamente `contado` o `credito`.")
        return

    bloques_productos = []
    for prod_nombre, cantidad in productos_matches:
        prod_clean = prod_nombre.strip()
        cant = int(cantidad)

        sql_block = f"""
        SELECT nv.id_venta, p.id_producto, {cant}, p.precio_venta
        FROM nueva_venta nv, (
            SELECT id_producto, precio_venta 
            FROM productos 
            WHERE LOWER(nombre) LIKE LOWER('%{prod_clean}%') 
            ORDER BY 
                CASE WHEN LOWER(nombre) = LOWER('{prod_clean}') THEN 1 ELSE 2 END,
                LENGTH(nombre) ASC 
            LIMIT 1
        ) p
        """
        bloques_productos.append(sql_block)

    query_productos_sql = "\nUNION ALL\n".join(bloques_productos)

    query_completa = f"""
    WITH cliente_existente AS (
        SELECT id_cliente FROM clientes WHERE LOWER(nombre) LIKE LOWER('%{cliente_nombre}%') LIMIT 1
    ),
    cliente_creado AS (
        INSERT INTO clientes (nombre)
        SELECT '{cliente_nombre}'
        WHERE NOT EXISTS (SELECT 1 FROM cliente_existente)
        RETURNING id_cliente
    ),
    cliente_final AS (
        SELECT id_cliente FROM cliente_existente UNION ALL SELECT id_cliente FROM cliente_creado
    ),
    nueva_venta AS (
        INSERT INTO ventas (id_cliente, tipo_pago)
        SELECT id_cliente, '{tipo_pago}' FROM cliente_final
        RETURNING id_venta
    ),
    insertar_detalles AS (
        INSERT INTO detalle_ventas (id_venta, id_producto, cantidad, precio_unitario)
        {query_productos_sql}
        RETURNING id_venta, (cantidad * precio_unitario) AS subtotal
    )
    SELECT id_venta, SUM(subtotal) AS total
    FROM insertar_detalles
    GROUP BY id_venta;
    """

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query_completa)
        res = cur.fetchone()
        conn.commit()
        
        id_venta_registrada = res[0]
        total_venta = res[1]
        cur.close()
        conn.close()

        respuesta = (
            f"🟢 **¡Venta #{id_venta_registrada} Registrada Exitosamente!**\n\n"
            f"👤 **Cliente:** {cliente_nombre}\n"
            f"💳 **Pago:** {tipo_pago.capitalize()}\n"
            f"📦 **Ítems procesados:** {len(productos_matches)}\n"
            f"💰 **Total Venta:** {formatear_cop(total_venta)}"
        )
        await update.message.reply_text(enviar_mensaje_seguro(respuesta), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar venta:** {str(e)}")

async def registrar_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    items = re.findall(r"-\s*(.+?),\s*(\d+),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)", texto)

    if not items:
        await update.message.reply_text("❌ **Formato de compra incorrecto.** Usa:\n/compra\n- Producto, Cantidad, CostoCompra, PrecioVenta", parse_mode="Markdown")
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        resúmenes = []

        for prod_nombre, cant_str, costo_str, precio_str in items:
            prod_clean = prod_nombre.strip()
            cant = int(cant_str)
            costo = float(costo_str)
            precio = float(precio_str)

            query_update = f"""
            UPDATE productos 
            SET stock_actual = stock_actual + {cant},
                costo_compra = {costo},
                precio_venta = {precio}
            WHERE LOWER(nombre) LIKE LOWER('%{prod_clean}%')
            RETURNING nombre, stock_actual;
            """
            cur.execute(query_update)
            res = cur.fetchone()

            if res:
                resúmenes.append(f"• **{res[0]}**: +{cant} unidades (Nuevo Stock: {res[1]})")
            else:
                resúmenes.append(f"⚠️ **{prod_clean}**: No encontrado en base de datos.")

        conn.commit()
        cur.close()
        conn.close()

        msg = "📦 **Reabastecimiento Registrado:**\n\n" + "\n".join(resúmenes)
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar compra:** {str(e)}")

# -------------------------------------------------------------------
# GENERADOR DE PDF MENSUAL
# -------------------------------------------------------------------
async def generar_pdf_mes():
    query = """
    SELECT v.id_venta, v.fecha_venta, c.nombre AS cliente, v.tipo_pago, SUM(dv.cantidad * dv.precio_unitario) AS total
    FROM ventas v
    JOIN clientes c ON v.id_cliente = c.id_cliente
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('month', v.fecha_venta) = DATE_TRUNC('month', CURRENT_DATE)
    GROUP BY v.id_venta, v.fecha_venta, c.nombre, v.tipo_pago
    ORDER BY v.fecha_venta ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    ventas = cur.fetchall()
    cur.close()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=18, textColor=colors.HexColor('#D81B60'), spaceAfter=12)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=10, textColor=colors.gray, spaceAfter=20)

    elements = [
        Paragraph("Soulcerón - Reporte Mensual de Ventas", titulo_style),
        Paragraph(f"Generado el: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
    ]

    tabla_data = [["ID Venta", "Fecha", "Cliente", "Tipo Pago", "Total"]]
    grand_total = 0

    for id_v, fecha, cl, pago, total in ventas:
        grand_total += total
        tabla_data.append([str(id_v), fecha.strftime('%Y-%m-%d'), str(cl), str(pago).capitalize(), formatear_cop(total)])

    tabla_data.append(["", "", "", "TOTAL MES:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[60, 80, 180, 80, 100])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8BBD0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#880E4F')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F5F5F5')),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

# -------------------------------------------------------------------
# HANDLER DE BOTONES (CALLBACK QUERY)
# -------------------------------------------------------------------
async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_ventas_hoy":
        cnt, total = await ventas_hoy_logic()
        msg = f"📊 **Ventas de Hoy (Acumulado en tiempo real):**\n\n🔢 Transacciones: {cnt}\n💰 Recaudado: {formatear_cop(total)}"
        await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_ventas_semana":
        cnt, total = await ventas_semana_logic()
        msg = f"📅 **Lo que va de Semana (Acumulado actual):**\n\n🔢 Transacciones: {cnt}\n💰 Recaudación total: {formatear_cop(total)}"
        await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_valorizacion":
        costo, venta = await valorizacion_logic()
        msg = f"🏢 **Valorización de Bodega:**\n\n💵 Invertido: {formatear_cop(costo)}\n📈 Valor Venta: {formatear_cop(venta)}"
        await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_stock_bajo":
        filas = await stock_bajo_logic()
        if not filas:
            await query.message.reply_text("✅ Inventario en niveles óptimos.", reply_markup=obtener_teclado_menu())
        else:
            lineas = ["⚠️ **Productos con Stock Bajo:**\n"]
            for n, s in filas:
                lineas.append(f"• **{n}**: {s} unidades")
            await query.message.reply_text(enviar_mensaje_seguro("\n".join(lineas)), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_reporte_pdf":
        await query.message.reply_text("🔄 Generando reporte PDF del mes...")
        pdf_buffer = await generar_pdf_mes()
        await query.message.reply_document(document=pdf_buffer, filename=f"Reporte_Soulceron_{datetime.now().strftime('%m_%Y')}.pdf")

    elif query.data == "btn_ayuda":
        await ayuda(update, context)

# -------------------------------------------------------------------
# TAREAS AUTOMÁTICAS (CIERRE DIARIO Y SEMANAL)
# -------------------------------------------------------------------
async def enviar_cierre_diario(bot):
    if CHAT_ID_ADMIN:
        try:
            cnt, total = await ventas_hoy_logic()
            msg = f"🔔 **CIERRE AUTOMÁTICO DEL DÍA (7:00 PM)** 🔔\n\n🔢 Ventas realizadas hoy: {cnt}\n💰 Total recaudado hoy: {formatear_cop(total)}"
            await bot.send_message(chat_id=CHAT_ID_ADMIN, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error en cierre diario automático: {e}")

async def enviar_cierre_semanal(bot):
    if CHAT_ID_ADMIN:
        try:
            cnt, total = await ventas_semana_logic()
            msg = f"📊 **BALANCE AUTOMÁTICO SEMANAL (DOMINGO 8:00 PM)** 📊\n\n🔢 Total transacciones semana: {cnt}\n💰 Recaudación total semana: {formatear_cop(total)}"
            await bot.send_message(chat_id=CHAT_ID_ADMIN, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error en cierre semanal automático: {e}")

# -------------------------------------------------------------------
# FLASK Y WEBHOOK
# -------------------------------------------------------------------
@web_app.route('/', methods=['GET'])
def home():
    return "Bot de Soulcerón Activo con Menú Interactivo."

@web_app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        json_data = request.get_json(force=True)
        
        async def process():
            ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
            
            ptb_app.add_handler(CommandHandler("start", start))
            ptb_app.add_handler(CommandHandler("menu", start))
            ptb_app.add_handler(CommandHandler("ayuda", ayuda))
            ptb_app.add_handler(CommandHandler("venta", registrar_venta))
            ptb_app.add_handler(CommandHandler("compra", registrar_compra))
            ptb_app.add_handler(CommandHandler("ventas_hoy", lambda u, c: manejar_callback(u, c)))
            ptb_app.add_handler(CommandHandler("ventas_semana", lambda u, c: manejar_callback(u, c)))
            
            ptb_app.add_handler(CallbackQueryHandler(manejar_callback))
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/venta"), registrar_venta))
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/compra"), registrar_compra))

            async with ptb_app:
                update = Update.de_json(json_data, ptb_app.bot)
                await ptb_app.process_update(update)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(process())
        finally:
            loop.close()
            
        return 'ok', 200

# Planificador para tareas automáticas
scheduler = AsyncIOScheduler()
# 1. Cierre Diario a las 7:00 PM (19:00 horas todos los días)
scheduler.add_job(lambda: asyncio.run(enviar_cierre_diario(ApplicationBuilder().token(TELEGRAM_TOKEN).build().bot)), 'cron', hour=19, minute=0)
# 2. Cierre Semanal a las 8:00 PM (20:00 horas todos los domingos)
scheduler.add_job(lambda: asyncio.run(enviar_cierre_semanal(ApplicationBuilder().token(TELEGRAM_TOKEN).build().bot)), 'cron', day_of_week='sun', hour=20, minute=0)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)
