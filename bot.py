import os
import re
import io
import logging
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
    return psycopg2.connect(DATABASE_URL, sslmode='require', connect_timeout=10)

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
    WHERE DATE(v.fecha) = CURRENT_DATE;
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
        DATE(v.fecha) AS fecha,
        TO_CHAR(v.fecha, 'TMDay') AS dia_nombre,
        COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS contado_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS credito_dia
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('week', v.fecha) = DATE_TRUNC('week', CURRENT_DATE)
    GROUP BY DATE(v.fecha), TO_CHAR(v.fecha, 'TMDay')
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
    WHERE DATE_TRUNC('week', v.fecha) = DATE_TRUNC('week', CURRENT_DATE);
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
# CONSULTA DE CLIENTE (/cliente)
# -------------------------------------------------------------------
async def consultar_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ **Indica el nombre del cliente.** Usa:\n`/cliente Nombre`", parse_mode="Markdown")
        return

    nombre_buscar = " ".join(args).strip()
    query = f"""
    SELECT 
        c.nombre,
        COUNT(DISTINCT v.id_venta) AS total_compras,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_contado,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_credito
    FROM clientes c
    LEFT JOIN ventas v ON c.id_cliente = v.id_cliente
    LEFT JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE LOWER(c.nombre) LIKE LOWER('%{nombre_buscar}%')
    GROUP BY c.id_cliente, c.nombre
    LIMIT 1;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        res = cur.fetchone()
        cur.close()
        conn.close()

        if not res or res[0] is None:
            await update.message.reply_text(f"🔍 **No se encontró ningún cliente registrado con el nombre** `{nombre_buscar}`.", parse_mode="Markdown")
            return

        msg = (
            f"👤 **Perfil de Cliente:** {res[0]}\n\n"
            f"🛍️ **Compras Realizadas:** {res[1]}\n"
            f"💵 **Total Comprado a Contado:** `{formatear_cop(res[2])}`\n"
            f"💳 **Total Comprado a Crédito:** `{formatear_cop(res[3])}`"
        )
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al consultar cliente:** {str(e)}")

# -------------------------------------------------------------------
# REGISTRO DE VENTAS (/venta) CON VERIFICACIÓN DE EXISTENCIA
# -------------------------------------------------------------------
async def registrar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text

    cliente_match = re.search(r"Cliente:\s*(.+)", texto, re.IGNORECASE)
    pago_match = re.search(r"Pago:\s*(.+)", texto, re.IGNORECASE)
    productos_matches = re.findall(r"-\s*(.+?),\s*(\d+)", texto)

    if not cliente_match or not pago_match or not productos_matches:
        await update.message.reply_text("❌ **Formato incorrecto.** Usa:\n/venta\nCliente: Nombre\nPago: contado\n- Producto, Cantidad", parse_mode="Markdown")
        return

    cliente_nombre = cliente_match.group(1).strip()
    tipo_pago = pago_match.group(1).strip().lower()

    if tipo_pago not in ['contado', 'credito']:
        await update.message.reply_text("❌ El tipo de pago debe ser exclusivamente `contado` o `credito`.", parse_mode="Markdown")
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Verificar qué productos existen y cuáles no
        productos_encontrados = []
        productos_no_encontrados = []

        for prod_nombre, cant in productos_matches:
            prod_clean = prod_nombre.strip()
            cantidad = int(cant)

            query_check = """
            SELECT id_producto, nombre, precio_venta 
            FROM productos 
            WHERE LOWER(nombre) LIKE LOWER(%s) 
            ORDER BY 
                CASE WHEN LOWER(nombre) = LOWER(%s) THEN 1 ELSE 2 END,
                LENGTH(nombre) ASC 
            LIMIT 1;
            """
            cur.execute(query_check, (f'%{prod_clean}%', prod_clean))
            prod_res = cur.fetchone()

            if prod_res:
                productos_encontrados.append({
                    'id_producto': prod_res[0],
                    'nombre_real': prod_res[1],
                    'cantidad': cantidad,
                    'precio_unitario': prod_res[2],
                    'subtotal': cantidad * prod_res[2]
                })
            else:
                productos_no_encontrados.append(prod_clean)

        # Si hay productos no encontrados, abortamos la venta y notificamos
        if productos_no_encontrados:
            cur.close()
            conn.close()
            lineas_error = ["🔴 **Venta cancelada. Los siguientes productos no existen en la base de datos:**\n"]
            for p_err in productos_no_encontrados:
                lineas_error.append(f"❌ `{p_err}`")
            lineas_error.append("\n💡 *Regístralos primero con `/compra` antes de venderlos.*")
            await update.message.reply_text("\n".join(lineas_error), parse_mode="Markdown")
            return

        # 2. Si todos existen, procedemos con la inserción de la venta
        query_cliente = """
        WITH cliente_existente AS (
            SELECT id_cliente FROM clientes WHERE LOWER(nombre) LIKE LOWER(%s) LIMIT 1
        ),
        cliente_creado AS (
            INSERT INTO clientes (nombre)
            SELECT %s
            WHERE NOT EXISTS (SELECT 1 FROM cliente_existente)
            RETURNING id_cliente
        )
        SELECT id_cliente FROM cliente_existente UNION ALL SELECT id_cliente FROM cliente_creado LIMIT 1;
        """
        cur.execute(query_cliente, (f'%{cliente_nombre}%', cliente_nombre))
        id_cliente = cur.fetchone()[0]

        query_nueva_venta = "INSERT INTO ventas (id_cliente, tipo_pago) VALUES (%s, %s) RETURNING id_venta;"
        cur.execute(query_nueva_venta, (id_cliente, tipo_pago))
        id_venta = cur.fetchone()[0]

        total_venta = 0
        lista_detalles_msg = []

        for item in productos_encontrados:
            query_detalle = """
            INSERT INTO detalle_ventas (id_venta, id_producto, cantidad, precio_unitario)
            VALUES (%s, %s, %s, %s);
            """
            cur.execute(query_detalle, (id_venta, item['id_producto'], item['cantidad'], item['precio_unitario']))
            
            # Actualizar stock del producto
            query_descuento = "UPDATE productos SET stock_actual = stock_actual - %s WHERE id_producto = %s;"
            cur.execute(query_descuento, (item['cantidad'], item['id_producto']))

            total_venta += item['subtotal']
            lista_detalles_msg.append(f"• **{item['nombre_real']}** x{item['cantidad']} — `{formatear_cop(item['subtotal'])}`")

        conn.commit()
        cur.close()
        conn.close()

        msg = (
            f"🎉 **¡Venta #{id_venta} Registrada Exitosamente!** 🎉\n\n"
            f"👤 **Cliente:** {cliente_nombre}\n"
            f"💳 **Método de Pago:** {tipo_pago.capitalize()}\n\n"
            f"📋 **Productos Procesados:**\n" + "\n".join(lista_detalles_msg) + "\n\n"
            f"💰 **TOTAL COBRADO:** `{formatear_cop(total_venta)}`"
        )
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar venta:** {str(e)}")

# -------------------------------------------------------------------
# REGISTRO DE COMPRAS (/compra) CON DETECCIÓN DE NUEVOS REGISTROS
# -------------------------------------------------------------------
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

            # Intentar actualizar producto existente
            query_update = """
            UPDATE productos 
            SET stock_actual = stock_actual + %s,
                costo_compra = %s,
                precio_venta = %s
            WHERE LOWER(nombre) LIKE LOWER(%s)
            RETURNING nombre, stock_actual;
            """
            cur.execute(query_update, (cant, costo, precio, f'%{prod_clean}%'))
            res = cur.fetchone()

            if res:
                resúmenes.append(f"• **{res[0]}** `[REABASTECIDO]`\n   └ +{cant} uds. (Nuevo Stock: `{res[1]}`) | Costo: `{formatear_cop(costo)}` | Venta: `{formatear_cop(precio)}`")
            else:
                # Si no existe, crearlo como NUEVO REGISTRO (Categoría 1 Maquillaje por defecto)
                query_insert = """
                INSERT INTO productos (nombre, stock_actual, costo_compra, precio_venta, id_categoria)
                VALUES (%s, %s, %s, %s, 1)
                RETURNING nombre, stock_actual;
                """
                cur.execute(query_insert, (prod_clean, cant, costo, precio))
                nuevo_res = cur.fetchone()
                resúmenes.append(f"✨ **{nuevo_res[0]}** `[NUEVO REGISTRO]`\n   └ Stock inicial: `{nuevo_res[1]}` uds. | Costo: `{formatear_cop(costo)}` | Venta: `{formatear_cop(precio)}`")

        conn.commit()
        cur.close()
        conn.close()

        msg = f"📦 **Resumen de Reabastecimiento / Compras:**\n\n" + "\n\n".join(resúmenes)
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar compra:** {str(e)}")

# -------------------------------------------------------------------
# GENERADOR DE PDF MENSUAL
# -------------------------------------------------------------------
def generar_pdf_mes_sync():
    query = """
    SELECT v.id_venta, v.fecha, c.nombre AS cliente, v.tipo_pago, SUM(dv.cantidad * dv.precio_unitario) AS total
    FROM ventas v
    JOIN clientes c ON v.id_cliente = c.id_cliente
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('month', v.fecha) = DATE_TRUNC('month', CURRENT_DATE)
    GROUP BY v.id_venta, v.fecha, c.nombre, v.tipo_pago
    ORDER BY v.fecha ASC;
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

    try:
        if query.data == "btn_ventas_hoy":
            cnt, total = ventas_hoy_sync()
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
            dias, totales = ventas_semana_sync()
            
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
            costo, venta = valorizacion_sync()
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
            filas = stock_bajo_sync()
            if not filas:
                await query.message.reply_text("✅ **No se encontraron productos de Maquillaje agotados. ¡Tu stock está al día!**", reply_markup=obtener_teclado_menu(), parse_mode="Markdown")
            else:
                lineas = ["🚫 **Productos de Maquillaje Agotados (0 uds.)**\n"]
                for n, s in filas:
                    lineas.append(f"• **{n}**")
                await query.message.reply_text(enviar_mensaje_seguro("\n".join(lineas)), reply_markup=obtener_teclado_menu(), parse_mode="Markdown")

        elif query.data == "btn_reporte_pdf":
            pdf_buffer = generar_pdf_mes_sync()
            
            if pdf_buffer is None:
                await query.message.reply_text("ℹ️ **No se encontraron ventas registradas durante este mes para generar el PDF.**", parse_mode="Markdown")
            else:
                await query.message.reply_document(
                    document=pdf_buffer, 
                    filename=f"Reporte_Soulceron_{datetime.now().strftime('%m_%Y')}.pdf",
                    caption="📄 Aquí tienes tu reporte PDF del mes listo para consultar."
                )

        elif query.data == "btn_ayuda":
            await ayuda(update, context)

    except Exception as e:
        logging.error(f"Error en callback: {e}")
        await query.message.reply_text(f"🔴 **Error al procesar la solicitud:** {str(e)}")

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
            
            import asyncio
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
            ptb_app.add_handler(CommandHandler("cliente", consultar_cliente))
            
            ptb_app.add_handler(CallbackQueryHandler(manejar_callback))
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/venta"), registrar_venta))
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/compra"), registrar_compra))
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/cliente"), consultar_cliente))

            async with ptb_app:
                update = Update.de_json(json_data, ptb_app.bot)
                await ptb_app.process_update(update)

        import asyncio
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
