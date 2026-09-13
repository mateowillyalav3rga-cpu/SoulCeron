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

from apscheduler.schedulers.background import BackgroundScheduler
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle
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
# MENÚ CON BOTONES INTERACTIVOS
# -------------------------------------------------------------------
def obtener_teclado_menu():
    keyboard = [
        [
            InlineKeyboardButton("📊 Ventas Hoy", callback_data="btn_ventas_hoy"),
            InlineKeyboardButton("📅 Balance Semanal", callback_data="btn_ventas_semana")
        ],
        [
            InlineKeyboardButton("🏢 Valorización Bodega", callback_data="btn_valorizacion"),
            InlineKeyboardButton("🚫 Productos Agotados", callback_data="btn_stock_bajo")
        ],
        [
            InlineKeyboardButton("📄 Reporte PDF Mes", callback_data="btn_reporte_pdf"),
            InlineKeyboardButton("❓ Ayuda / Formatos", callback_data="btn_ayuda")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✨ **¡Hola! Bienvenido al asistente de Soulcerón** ✨\n\n"
        "Estoy listo para ayudarte a gestionar tus ventas, inventario y finanzas.\n\n"
        "👇 *Selecciona una opción del menú para comenzar:*"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🤖 **Formatos de Registro y Consultas**\n\n"
        "📝 **Registrar Venta:**\n"
        "```text\n"
        "/venta\n"
        "Cliente: Nombre Apellido\n"
        "Pago: contado\n"
        "- Pomo, 1\n"
        "```\n\n"
        "📦 **Registrar Compra / Reabastecimiento:**\n"
        "```text\n"
        "/compra\n"
        "- Pomo, 50, 1300, 3000\n"
        "```\n\n"
        "🔍 **Consultar Cliente:**\n"
        "`/cliente Nombre`"
    )
    if update.message:
        await update.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

# -------------------------------------------------------------------
# LÓGICA DE CONSULTAS SQL
# -------------------------------------------------------------------
def ventas_hoy_sync():
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

def ventas_semana_sync():
    query_dias = """
    SELECT 
        DATE(v.fecha_venta) AS fecha,
        TO_CHAR(v.fecha_venta, 'TMDay') AS dia_nombre,
        COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS contado_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS credito_dia
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('week', v.fecha_venta) = DATE_TRUNC('week', CURRENT_DATE)
    GROUP BY DATE(v.fecha_venta), TO_CHAR(v.fecha_venta, 'TMDay')
    ORDER BY fecha ASC;
    """
    
    query_totales = """
    SELECT 
        COUNT(DISTINCT v.id_venta) AS total_ventas,
        COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_contado,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_credito,
        COALESCE(SUM(dv.cantidad * (dv.precio_unitario - p.costo_compra)), 0) AS ganancia_total
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    JOIN productos p ON dv.id_producto = p.id_producto
    WHERE DATE_TRUNC('week', v.fecha_venta) = DATE_TRUNC('week', CURRENT_DATE);
    """
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query_dias)
    dias = cur.fetchall()
    
    cur.execute(query_totales)
    totales = cur.fetchone()
    
    cur.close()
    conn.close()
    
    return dias, totales

def stock_bajo_sync():
    query = """
    SELECT nombre, stock_actual 
    FROM productos 
    WHERE id_categoria = 1 AND stock_actual = 0 
    ORDER BY nombre ASC
    LIMIT 20;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas

def valorizacion_sync():
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
        def ejecutar_venta_sync():
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(query_completa)
            res = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            return res

        res = await asyncio.to_thread(ejecutar_venta_sync)
        id_venta_registrada = res[0]
        total_venta = res[1]

        respuesta = (
            f"🎉 **¡Venta #{id_venta_registrada} Registrada!** 🎉\n\n"
            f"👤 **Cliente:** {cliente_nombre}\n"
            f"💳 **Método de Pago:** {tipo_pago.capitalize()}\n"
            f"📦 **Ítems vendidos:** {len(productos_matches)}\n\n"
            f"💰 **Total Cobrado:** `{formatear_cop(total_venta)}`"
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
        def ejecutar_compra_sync():
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
                    resúmenes.append(f"• **{res[0]}**: +{cant} uds. (Nuevo Stock: `{res[1]}`)")
                else:
                    resúmenes.append(f"⚠️ **{prod_clean}**: No encontrado en la base de datos.")

            conn.commit()
            cur.close()
            conn.close()
            return resúmenes

        resúmenes = await asyncio.to_thread(ejecutar_compra_sync)
        msg = f"📦 **Reabastecimiento de Inventario**\n\n" + "\n\n".join(resúmenes)
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar compra:** {str(e)}")

# -------------------------------------------------------------------
# GENERADOR DE PDF MENSUAL
# -------------------------------------------------------------------
def generar_pdf_mes_sync():
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

    if not ventas:
        return None

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
        cnt, total = await asyncio.to_thread(ventas_hoy_sync)
        if cnt == 0:
            msg = "ℹ️ **No se encontraron ventas registradas en el día de hoy.**"
        else:
            msg = (
                f"📊 **Ventas del Día de Hoy**\n\n"
                f"🔢 **Transacciones:** {cnt}\n"
                f"💰 **Total Recaudado:** `{formatear_cop(total)}`"
            )
        await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_ventas_semana":
        dias, totales = await asyncio.to_thread(ventas_semana_sync)
        
        if not totales or totales[0] == 0:
            msg_final = "ℹ️ **No se encontraron ventas registradas en lo que va de esta semana.**"
        else:
            cnt, total, contado, credito, ganancia = totales
            mensaje = ["📅 **Balance de la Semana**\n"]
            mensaje.append("📆 **Desglose Diario:**\n")
            
            for fecha, dia_nombre, total_dia, contado_dia, credito_dia in dias:
                dia_clean = dia_nombre.strip().capitalize()
                fecha_str = fecha.strftime('%d/%m')
                mensaje.append(
                    f"🔹 **{dia_clean}** ({fecha_str})\n"
                    f"   └ Total: `{formatear_cop(total_dia)}`  *(💵 `{formatear_cop(contado_dia)}` | 💳 `{formatear_cop(credito_dia)}`)*\n"
                )
            
            mensaje.append("\n📊 **Resumen General:**\n")
            mensaje.append(f"🔢 **Transacciones:** {cnt}")
            mensaje.append(f"💵 **Total Contado:** `{formatear_cop(contado)}`")
            mensaje.append(f"💳 **Total Crédito:** `{formatear_cop(credito)}`")
            mensaje.append(f"💰 **Recaudación Total:** `{formatear_cop(total)}`")
            mensaje.append(f"📈 **Ganancia Neta Estimada:** `{formatear_cop(ganancia)}`")

            msg_final = "\n".join(mensaje)
        await query.message.reply_text(enviar_mensaje_seguro(msg_final), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_valorizacion":
        costo, venta = await asyncio.to_thread(valorizacion_sync)
        if costo == 0 and venta == 0:
            msg = "ℹ️ **No se encontraron productos registrados en inventario para calcular la valorización.**"
        else:
            msg = (
                f"🏢 **Valorización de Bodega**\n\n"
                f"💵 **Costo Invertido:** `{formatear_cop(costo)}`\n"
                f"📈 **Valor Potencial de Venta:** `{formatear_cop(venta)}`"
            )
        await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_stock_bajo":
        filas = await asyncio.to_thread(stock_bajo_sync)
        if not filas:
            await query.message.reply_text("✅ **No se encontraron productos de Maquillaje agotados. ¡Tu stock está al día!**", reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
        else:
            lineas = ["🚫 **Productos de Maquillaje Agotados (0 uds.)**\n"]
            for n, s in filas:
                lineas.append(f"• **{n}**")
            await query.message.reply_text(enviar_mensaje_seguro("\n".join(lineas)), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    elif query.data == "btn_reporte_pdf":
        msg_espera = await query.message.reply_text("🔄 Consultando base de datos...")
        pdf_buffer = await asyncio.to_thread(generar_pdf_mes_sync)
        
        if pdf_buffer is None:
            await msg_espera.edit_text("ℹ️ **No se encontraron ventas registradas durante este mes para generar el PDF.**", parse_mode="Markdown")
        else:
            await msg_espera.edit_text("🔄 Generando reporte PDF del mes...")
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=pdf_buffer, 
                filename=f"Reporte_Soulceron_{datetime.now().strftime('%m_%Y')}.pdf",
                caption="📄 Aquí tienes tu reporte PDF del mes listo para consultar."
            )
            await msg_espera.delete()

    elif query.data == "btn_ayuda":
        await ayuda(update, context)

# -------------------------------------------------------------------
# TAREAS AUTOMÁTICAS PROGRAMADAS
# -------------------------------------------------------------------
def tarea_cierre_diario():
    if CHAT_ID_ADMIN and TELEGRAM_TOKEN:
        try:
            cnt, total = ventas_hoy_sync()
            msg = (
                f"🔔 **Cierre de Caja Automático (7:00 PM)** 🔔\n\n"
                f"🔢 **Ventas realizadas hoy:** {cnt}\n"
                f"💰 **Total recaudado:** `{formatear_cop(total)}`"
            )
            
            async def send():
                ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
                async with ptb_app:
                    await ptb_app.bot.send_message(chat_id=CHAT_ID_ADMIN, text=msg, parse_mode="Markdown")
            
            asyncio.run(send())
        except Exception as e:
            logging.error(f"Error en cierre diario automático: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(tarea_cierre_diario, 'cron', hour=19, minute=0)
scheduler.start()

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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)
