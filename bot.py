import os
import re
import io
import json
import logging
import psycopg2
import urllib.request
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHAT_ID_ADMIN = os.getenv("CHAT_ID_ADMIN")

# IDs oficiales de administradores
ID_ESPOSA = "2059542689"
ID_ESPOSO = "5197161394"

def obtener_lista_admins():
    admins = [ID_ESPOSO, ID_ESPOSA]
    if CHAT_ID_ADMIN:
        for cid in CHAT_ID_ADMIN.split(","):
            cid_clean = cid.strip().replace('"', '').replace("'", "")
            # Ignorar el ID viejo (2074541555) si aún existe en variables de entorno de Render
            if cid_clean and cid_clean not in admins and cid_clean != "2074541555":
                admins.append(cid_clean)
    return admins

COLOMBIA_TZ = ZoneInfo('America/Bogota')

web_app = Flask(__name__)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require', connect_timeout=10)

def enviar_mensaje_api(chat_id: str, texto: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": str(chat_id),
        "text": texto,
        "parse_mode": "HTML"
    }).encode('utf-8')
    
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            logging.info(f"Envío API Telegram a {chat_id}: status {response.status}")
    except urllib.error.HTTPError as e:
        error_resp = e.read().decode('utf-8')
        logging.error(f"Error HTTP {e.code} enviando a {chat_id}: {error_resp}")
    except Exception as e:
        logging.error(f"Error enviando mensaje API Telegram a {chat_id}: {e}")

def enviar_documento_api(chat_id: str, document_buffer, filename: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    
    document_buffer.seek(0)
    file_bytes = document_buffer.read()
    
    body = bytearray()
    
    # Campo chat_id
    body.extend(f"--{boundary}\r\n".encode('utf-8'))
    body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode('utf-8'))
    body.extend(f"{chat_id}\r\n".encode('utf-8'))
    
    # Campo caption
    if caption:
        body.extend(f"--{boundary}\r\n".encode('utf-8'))
        body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode('utf-8'))
        body.extend(f"{caption}\r\n".encode('utf-8'))
        
    # Campo parse_mode
    body.extend(f"--{boundary}\r\n".encode('utf-8'))
    body.extend(f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'.encode('utf-8'))
    body.extend("HTML\r\n".encode('utf-8'))
    
    # Campo document
    body.extend(f"--{boundary}\r\n".encode('utf-8'))
    body.extend(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode('utf-8'))
    body.extend("Content-Type: application/pdf\r\n\r\n".encode('utf-8'))
    body.extend(file_bytes)
    body.extend("\r\n".encode('utf-8'))
    
    body.extend(f"--{boundary}--\r\n".encode('utf-8'))
    
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            logging.info(f"Envío Documento API Telegram a {chat_id}: status {response.status}")
    except urllib.error.HTTPError as e:
        error_resp = e.read().decode('utf-8')
        logging.error(f"Error HTTP Documento {e.code} a {chat_id}: {error_resp}")
    except Exception as e:
        logging.error(f"Error enviando documento API Telegram a {chat_id}: {e}")

def enviar_mensaje_seguro(texto: str, max_length: int = 4000) -> str:
    if len(texto) > max_length:
        return texto[:max_length] + "\n\n⚠️ <i>(Resultado recortado por longitud)</i>"
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
    chat_id = update.effective_chat.id
    msg = (
        f"✨ <b>¡Hola! Bienvenido al asistente de Soulcerón</b> ✨\n\n"
        f"📱 <b>Tu ID de Chat:</b> <code>{chat_id}</code>\n\n"
        "Estoy listo para ayudarte a gestionar tus ventas, inventario y finanzas.\n\n"
        "👇 <i>Selecciona una opción del menú para comenzar:</i>"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=obtener_teclado_menu(), parse_mode="HTML")

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🤖 <b>Formatos de Registro y Consultas</b>\n\n"
        "📝 <b>Registrar Venta:</b>\n"
        "<pre>\n"
        "/venta\n"
        "Cliente: Nombre Apellido\n"
        "Pago: contado\n"
        "- Pomo, 1\n"
        "</pre>\n\n"
        "📦 <b>Registrar Compra / Reabastecimiento:</b>\n"
        "<pre>\n"
        "/compra\n"
        "- Pomo, 50, 1300, 3000\n"
        "</pre>\n\n"
        "🔍 <b>Consultar Cliente:</b>\n"
        "<code>/cliente Nombre</code>"
    )
    if update.message:
        await update.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.reply_text(mensaje, reply_markup=obtener_teclado_menu(), parse_mode="HTML")

# -------------------------------------------------------------------
# LÓGICA DE CONSULTAS SQL
# -------------------------------------------------------------------
def ventas_hoy_sync():
    query = """
    SELECT COUNT(DISTINCT v.id_venta) AS total_ventas, COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE(CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota');
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
        DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') AS fecha,
        TO_CHAR(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota', 'TMDay') AS dia_nombre,
        COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS contado_dia,
        COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS credito_dia
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('week', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('week', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')
    GROUP BY DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota'), TO_CHAR(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota', 'TMDay')
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
    WHERE DATE_TRUNC('week', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('week', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota');
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
    SELECT p.nombre, p.stock_actual, p.costo_compra, p.precio_venta, COALESCE(pr.nombre_empresa, 'Sin Proveedor') AS proveedor
    FROM productos p
    LEFT JOIN proveedores pr ON p.id_proveedor = pr.id_proveedor
    WHERE p.stock_actual = 0 
    ORDER BY p.nombre ASC;
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

def inventario_completo_sync():
    query = """
    SELECT p.nombre, p.stock_actual, p.costo_compra, p.precio_venta, COALESCE(pr.nombre_empresa, 'Sin Proveedor') AS proveedor
    FROM productos p
    LEFT JOIN proveedores pr ON p.id_proveedor = pr.id_proveedor
    ORDER BY p.nombre ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas

# -------------------------------------------------------------------
# CONSULTA DE CLIENTE (/cliente)
# -------------------------------------------------------------------
async def consultar_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ <b>Indica el nombre del cliente.</b> Usa:\n<code>/cliente Nombre</code>", parse_mode="HTML")
        return

    nombre_buscar = " ".join(args).strip()
    query = f"""
    SELECT 
        nombre,
        total_compras,
        total_contado,
        total_credito,
        categoria_calculada,
        ultima_compra,
        dias_sin_comprar
    FROM vista_clientes_segmentados
    WHERE LOWER(nombre) LIKE LOWER('%{nombre_buscar}%')
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
            await update.message.reply_text(f"🔍 <b>No se encontró ningún cliente registrado con el nombre</b> <code>{nombre_buscar}</code>.", parse_mode="HTML")
            return

        nombre_cl, compras, contado, credito, categoria, ult_compra, dias_sin = res
        ult_compra_str = ult_compra.strftime('%d/%m/%Y') if ult_compra else "Sin compras previas"
        dias_str = f"{int(dias_sin)} días" if dias_sin is not None else "N/A"

        msg = (
            f"👤 <b>Perfil de Cliente:</b> {nombre_cl}\n"
            f"🏷️ <b>Segmento:</b> <code>{categoria}</code>\n\n"
            f"🛍️ <b>Compras Realizadas:</b> {compras}\n"
            f"💵 <b>Total Contado:</b> <code>{formatear_cop(contado)}</code>\n"
            f"💳 <b>Total Crédito:</b> <code>{formatear_cop(credito)}</code>\n\n"
            f"📅 <b>Última Compra:</b> {ult_compra_str}\n"
            f"⏳ <b>Tiempo Transcurrido:</b> <code>{dias_str}</code>"
        )
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"🔴 <b>Error al consultar cliente:</b> {str(e)}")

# -------------------------------------------------------------------
# REGISTRO DE VENTAS Y COMPRAS
# -------------------------------------------------------------------
async def registrar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    cliente_match = re.search(r"Cliente:\s*(.+)", texto, re.IGNORECASE)
    pago_match = re.search(r"Pago:\s*(.+)", texto, re.IGNORECASE)
    productos_matches = re.findall(r"-\s*(.+?),\s*(\d+)", texto)

    if not cliente_match or not pago_match or not productos_matches:
        await update.message.reply_text("❌ <b>Formato incorrecto.</b> Usa:\n/venta\nCliente: Nombre\nPago: contado\n- Producto, Cantidad", parse_mode="HTML")
        return

    cliente_nombre = cliente_match.group(1).strip()
    tipo_pago = pago_match.group(1).strip().lower()

    if tipo_pago not in ['contado', 'credito']:
        await update.message.reply_text("❌ El tipo de pago debe ser exclusivamente <code>contado</code> o <code>credito</code>.", parse_mode="HTML")
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()

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

        if productos_no_encontrados:
            cur.close()
            conn.close()
            lineas_error = ["🔴 <b>Venta cancelada. Los siguientes productos no existen en la base de datos:</b>\n"]
            for p_err in productos_no_encontrados:
                lineas_error.append(f"❌ <code>{p_err}</code>")
            lineas_error.append("\n💡 <i>Regístralos primero con <code>/compra</code> antes de venderlos.</i>")
            await update.message.reply_text("\n".join(lineas_error), parse_mode="HTML")
            return

        query_check_cliente = "SELECT id_cliente FROM clientes WHERE LOWER(nombre) LIKE LOWER(%s) LIMIT 1;"
        cur.execute(query_check_cliente, (f'%{cliente_nombre}%',))
        res_c = cur.fetchone()

        if res_c:
            id_cliente = res_c[0]
            etiqueta_cliente = "👤 <code>[CLIENTE REGISTRADO]</code>"
        else:
            query_add_cliente = "INSERT INTO clientes (nombre) VALUES (%s) RETURNING id_cliente;"
            cur.execute(query_add_cliente, (cliente_nombre,))
            id_cliente = cur.fetchone()[0]
            etiqueta_cliente = "✨ <code>[NUEVO CLIENTE]</code>"

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
            
            query_descuento = "UPDATE productos SET stock_actual = stock_actual - %s WHERE id_producto = %s;"
            cur.execute(query_descuento, (item['cantidad'], item['id_producto']))

            total_venta += item['subtotal']
            lista_detalles_msg.append(f"• <b>{item['nombre_real']}</b> x{item['cantidad']} — <code>{formatear_cop(item['subtotal'])}</code>")

        conn.commit()
        cur.close()
        conn.close()

        msg = (
            f"🎉 <b>¡Venta #{id_venta} Registrada Exitosamente!</b> 🎉\n\n"
            f"👤 <b>Cliente:</b> {cliente_nombre} {etiqueta_cliente}\n"
            f"💳 <b>Método de Pago:</b> {tipo_pago.capitalize()}\n\n"
            f"📋 <b>Productos Procesados:</b>\n" + "\n".join(lista_detalles_msg) + "\n\n"
            f"💰 <b>TOTAL COBRADO:</b> <code>{formatear_cop(total_venta)}</code>"
        )
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"🔴 <b>Error al registrar venta:</b> {str(e)}")

async def registrar_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    items = re.findall(r"-\s*(.+?),\s*(\d+),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)", texto)

    if not items:
        await update.message.reply_text("❌ <b>Formato de compra incorrecto.</b> Usa:\n/compra\n- Producto, Cantidad, CostoCompra, PrecioVenta", parse_mode="HTML")
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
                resúmenes.append(f"• <b>{res[0]}</b> <code>[REABASTECIDO]</code>\n   └ +{cant} uds. (Nuevo Stock: <code>{res[1]}</code>) | Costo: <code>{formatear_cop(costo)}</code> | Venta: <code>{formatear_cop(precio)}</code>")
            else:
                query_insert = """
                INSERT INTO productos (nombre, stock_actual, costo_compra, precio_venta, id_categoria)
                VALUES (%s, %s, %s, %s, 1)
                RETURNING nombre, stock_actual;
                """
                cur.execute(query_insert, (prod_clean, cant, costo, precio))
                nuevo_res = cur.fetchone()
                resúmenes.append(f"✨ <b>{nuevo_res[0]}</b> <code>[NUEVO REGISTRO]</code>\n   └ Stock inicial: <code>{nuevo_res[1]}</code> uds. | Costo: <code>{formatear_cop(costo)}</code> | Venta: <code>{formatear_cop(precio)}</code>")

        conn.commit()
        cur.close()
        conn.close()

        msg = f"📦 <b>Resumen de Reabastecimiento / Compras:</b>\n\n" + "\n\n".join(resúmenes)
        await update.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"🔴 <b>Error al registrar compra:</b> {str(e)}")

# -------------------------------------------------------------------
# COMANDO DE PRUEBA DE NOTIFICACIONES
# -------------------------------------------------------------------
async def probar_notificaciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = obtener_lista_admins()
    await update.message.reply_text(f"🧪 <b>Iniciando prueba de notificaciones automáticas...</b>\nDestinatarios: <code>{admins}</code>", parse_mode="HTML")
    tarea_saludo_manana()
    tarea_cierre_diario()
    tarea_cierre_semanal()
    await update.message.reply_text("✅ <b>Prueba ejecutada exitosamente.</b> Revisa los chats.")

# -------------------------------------------------------------------
# GENERADORES DE PDF (DIARIO, MENSUAL, SEMANAL, BODEGA Y AGOTADOS)
# -------------------------------------------------------------------
def generar_pdf_dia_sync():
    query = """
    SELECT v.id_venta, v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota' AS fecha, c.nombre AS cliente, v.tipo_pago, p.nombre AS producto, dv.cantidad, dv.precio_unitario, (dv.cantidad * dv.precio_unitario) AS subtotal
    FROM ventas v
    JOIN clientes c ON v.id_cliente = c.id_cliente
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    JOIN productos p ON dv.id_producto = p.id_producto
    WHERE DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE(CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')
    ORDER BY v.fecha ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    detalles = cur.fetchall()
    cur.close()
    conn.close()

    if not detalles:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#880E4F'), spaceAfter=8)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.gray, spaceAfter=15)

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte Diario de Ventas", titulo_style),
        Paragraph(f"Cierre de Día - Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
    ]

    tabla_data = [["ID", "Hora", "Cliente", "Pago", "Producto", "Cant", "Subtotal"]]
    grand_total = 0

    for id_v, fecha, cl, pago, prod, cant, precio, subtotal in detalles:
        grand_total += subtotal
        tabla_data.append([
            str(id_v), 
            fecha.strftime('%H:%M'), 
            str(cl), 
            str(pago).capitalize(), 
            str(prod), 
            str(cant), 
            formatear_cop(subtotal)
        ])

    tabla_data.append(["", "", "", "", "", "TOTAL:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[35, 55, 120, 60, 150, 35, 85])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8BBD0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#880E4F')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F5F5F5')),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def generar_pdf_mes_sync(mes_offset=0):
    query = """
    SELECT v.id_venta, v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota' AS fecha, c.nombre AS cliente, v.tipo_pago, SUM(dv.cantidad * dv.precio_unitario) AS total
    FROM ventas v
    JOIN clientes c ON v.id_cliente = c.id_cliente
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE_TRUNC('month', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota' - INTERVAL '%s month')
    GROUP BY v.id_venta, v.fecha, c.nombre, v.tipo_pago
    ORDER BY v.fecha ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query % mes_offset)
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

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte Mensual de Ventas", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
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

def generar_pdf_semana_sync():
    query = """
    SELECT v.id_venta, v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota' AS fecha, c.nombre AS cliente, v.tipo_pago, p.nombre AS producto, dv.cantidad, dv.precio_unitario, (dv.cantidad * dv.precio_unitario) AS subtotal
    FROM ventas v
    JOIN clientes c ON v.id_cliente = c.id_cliente
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    JOIN productos p ON dv.id_producto = p.id_producto
    WHERE DATE_TRUNC('week', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('week', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')
    ORDER BY v.fecha ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    detalles = cur.fetchall()
    cur.close()
    conn.close()

    if not detalles:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#880E4F'), spaceAfter=8)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.gray, spaceAfter=15)

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Balance Semanal Detallado", titulo_style),
        Paragraph(f"Semana en curso - Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
    ]

    tabla_data = [["ID", "Fecha", "Cliente", "Pago", "Producto", "Cant", "Subtotal"]]
    grand_total = 0

    for id_v, fecha, cl, pago, prod, cant, precio, subtotal in detalles:
        grand_total += subtotal
        tabla_data.append([
            str(id_v), 
            fecha.strftime('%d/%m'), 
            str(cl), 
            str(pago).capitalize(), 
            str(prod), 
            str(cant), 
            formatear_cop(subtotal)
        ])

    tabla_data.append(["", "", "", "", "", "TOTAL:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[35, 55, 120, 60, 150, 35, 85])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8BBD0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#880E4F')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F5F5F5')),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def generar_pdf_valorizacion_sync():
    productos = inventario_completo_sync()
    if not productos:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#880E4F'), spaceAfter=8)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.gray, spaceAfter=15)

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Valorización Completa de Bodega", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
    ]

    tabla_data = [["Producto", "Stock", "Costo U.", "Costo Total", "Precio V.", "Venta Total", "Proveedor"]]
    total_costo_inv = 0
    total_venta_pot = 0

    for nombre, stock, costo, precio, proveedor in productos:
        costo_total = stock * costo
        venta_total = stock * precio
        total_costo_inv += costo_total
        total_venta_pot += venta_total

        tabla_data.append([
            str(nombre),
            str(stock),
            formatear_cop(costo),
            formatear_cop(costo_total),
            formatear_cop(precio),
            formatear_cop(venta_total),
            str(proveedor)
        ])

    ganancia_potencial = total_venta_pot - total_costo_inv
    tabla_data.append(["TOTALES:", "", "", formatear_cop(total_costo_inv), "", formatear_cop(total_venta_pot), f"Ganancia: {formatear_cop(ganancia_potencial)}"])

    t = Table(tabla_data, colWidths=[120, 35, 65, 75, 65, 75, 69])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8BBD0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#880E4F')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F5F5F5')),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def generar_pdf_agotados_sync():
    agotados = stock_bajo_sync()
    if not agotados:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#D81B60'), spaceAfter=8)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.gray, spaceAfter=15)

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte de Productos Agotados", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style)
    ]

    tabla_data = [["Producto", "Stock", "Costo U.", "Precio Venta", "Proveedor"]]
    
    for nombre, stock, costo, precio, proveedor in agotados:
        tabla_data.append([
            str(nombre),
            str(stock),
            formatear_cop(costo),
            formatear_cop(precio),
            str(proveedor)
        ])

    t = Table(tabla_data, colWidths=[185, 45, 85, 85, 150])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8BBD0')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#880E4F')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
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
                msg = "ℹ️ <b>No se encontraron ventas registradas en el día de hoy.</b>"
            else:
                msg = (
                    f"📊 <b>Ventas del Día de Hoy</b>\n\n"
                    f"🔢 <b>Transacciones:</b> {cnt}\n"
                    f"💰 <b>Total Recaudado:</b> <code>{formatear_cop(total)}</code>"
                )
            await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")

        elif query.data == "btn_ventas_semana":
            dias, totales = ventas_semana_sync()
            
            if not totales or totales[0] == 0:
                msg_final = "ℹ️ <b>No se encontraron ventas registradas en lo que va de esta semana.</b>"
                await query.message.reply_text(enviar_mensaje_seguro(msg_final), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
            else:
                cnt, total, contado, credito, ganancia = totales
                mensaje = ["📅 <b>Balance de la Semana</b>\n"]
                mensaje.append("📆 <b>Desglose Diario:</b>\n")
                
                for fecha, dia_nombre, total_dia, contado_dia, credito_dia in dias:
                    dia_clean = dia_nombre.strip().capitalize()
                    fecha_str = fecha.strftime('%d/%m')
                    mensaje.append(
                        f"🔹 <b>{dia_clean}</b> ({fecha_str})\n"
                        f"   └ Total: <code>{formatear_cop(total_dia)}</code>  *(💵 <code>{formatear_cop(contado_dia)}</code> | 💳 <code>{formatear_cop(credito_dia)}</code>)*\n"
                    )
                
                mensaje.append("\n📊 <b>Resumen General:</b>\n")
                mensaje.append(f"🔢 <b>Transacciones:</b> {cnt}")
                mensaje.append(f"💵 <b>Total Contado:</b> <code>{formatear_cop(contado)}</code>")
                mensaje.append(f"💳 <b>Total Crédito:</b> <code>{formatear_cop(credito)}</code>")
                mensaje.append(f"💰 <b>Recaudación Total:</b> <code>{formatear_cop(total)}</code>")
                mensaje.append(f"📈 <b>Ganancia Neta Estimada:</b> <code>{formatear_cop(ganancia)}</code>\n")
                mensaje.append("📄 <i>Adjunto encontrarás el reporte PDF detallado de la semana.</i>")

                msg_final = "\n".join(mensaje)
                pdf_buffer = generar_pdf_semana_sync()

                now_co = datetime.now(COLOMBIA_TZ)
                await query.message.reply_text(enviar_mensaje_seguro(msg_final), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Balance_Semanal_{now_co.strftime('%d_%m_%Y')}.pdf"
                    )

        elif query.data == "btn_valorizacion":
            costo, venta = valorizacion_sync()
            ganancia_est = venta - costo
            if costo == 0 and venta == 0:
                msg = "ℹ️ <b>No se encontraron productos registrados en inventario para calcular la valorización.</b>"
                await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
            else:
                msg = (
                    f"🏢 <b>Valorización de Bodega</b>\n\n"
                    f"💵 <b>Costo Invertido:</b> <code>{formatear_cop(costo)}</code>\n"
                    f"📈 <b>Valor Potencial de Venta:</b> <code>{formatear_cop(venta)}</code>\n"
                    f"💎 <b>Ganancia Potencial Estimada:</b> <code>{formatear_cop(ganancia_est)}</code>\n\n"
                    f"📄 <i>Adjunto encontrarás el reporte PDF completo del inventario.</i>"
                )
                pdf_buffer = generar_pdf_valorizacion_sync()
                now_co = datetime.now(COLOMBIA_TZ)
                await query.message.reply_text(enviar_mensaje_seguro(msg), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Valorizacion_Bodega_{now_co.strftime('%d_%m_%Y')}.pdf"
                    )

        elif query.data == "btn_stock_bajo":
            filas = stock_bajo_sync()
            if not filas:
                await query.message.reply_text("✅ <b>No se encontraron productos agotados. ¡Tu stock está al día!</b>", reply_markup=obtener_teclado_menu(), parse_mode="HTML")
            else:
                lineas = [
                    "🚫 <b>Productos Totalmente Agotados (0 uds.)</b>\n",
                    "📄 <i>Adjunto encontrarás el reporte PDF con la lista completa.</i>"
                ]
                for n, s, c, p, prov in filas:
                    lineas.append(f"• <b>{n}</b>")
                
                pdf_buffer = generar_pdf_agotados_sync()
                now_co = datetime.now(COLOMBIA_TZ)
                await query.message.reply_text(enviar_mensaje_seguro("\n".join(lineas)), reply_markup=obtener_teclado_menu(), parse_mode="HTML")
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Productos_Agotados_{now_co.strftime('%d_%m_%Y')}.pdf"
                    )

        elif query.data == "btn_reporte_pdf":
            pdf_buffer = generar_pdf_mes_sync(mes_offset=0)
            now_co = datetime.now(COLOMBIA_TZ)
            if pdf_buffer is None:
                await query.message.reply_text("ℹ️ <b>No se encontraron ventas registradas durante este mes para generar el PDF.</b>", parse_mode="HTML")
            else:
                await query.message.reply_document(
                    document=pdf_buffer, 
                    filename=f"Reporte_Mes_{now_co.strftime('%m_%Y')}.pdf",
                    caption="📄 Aquí tienes tu reporte PDF del mes en curso."
                )

        elif query.data == "btn_ayuda":
            await ayuda(update, context)

    except Exception as e:
        logging.error(f"Error en callback: {e}")
        await query.message.reply_text(f"🔴 <b>Error al procesar la solicitud:</b> {str(e)}")

# -------------------------------------------------------------------
# TAREAS AUTOMÁTICAS PROGRAMADAS (CON SINTAXIS HTML SEGUIRA)
# -------------------------------------------------------------------
def tarea_saludo_manana():
    if TELEGRAM_TOKEN:
        try:
            admins = obtener_lista_admins()
            for admin_id in admins:
                if str(admin_id) == ID_ESPOSA:
                    msg = "☀️ <b>¡Buenos días!</b> ☀️\n\nRecuerda que estoy aquí para ayudarte a llevar tu negocio y vamos con toda el día de hoy, <b>Mi barrigona hermosa</b> 💖✨"
                else:
                    msg = "☀️ <b>¡Buenos días!</b> ☀️\n\nRecuerda que estoy aquí para ayudarte a llevar tu negocio y vamos con toda el día de hoy 💪✨"
                
                enviar_mensaje_api(admin_id, msg)
        except Exception as e:
            logging.error(f"Error en saludo de la mañana: {e}")

def tarea_cierre_diario():
    if TELEGRAM_TOKEN:
        try:
            cnt, total = ventas_hoy_sync()
            msg = (
                f"🔔 <b>Cierre de Caja Automático (7:00 PM)</b> 🔔\n\n"
                f"🔢 <b>Ventas realizadas hoy:</b> {cnt}\n"
                f"💰 <b>Total recaudado:</b> <code>{formatear_cop(total)}</code>\n\n"
                f"📄 <i>Adjunto encontrarás el reporte PDF con el desglose del día.</i>"
            )
            pdf_buffer = generar_pdf_dia_sync()
            now_co = datetime.now(COLOMBIA_TZ)
            admins = obtener_lista_admins()

            for admin_id in admins:
                enviar_mensaje_api(admin_id, msg)
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    enviar_documento_api(
                        admin_id,
                        pdf_buffer,
                        f"Reporte_Diario_{now_co.strftime('%d_%m_%Y')}.pdf",
                        "📄 Reporte PDF Diario"
                    )
        except Exception as e:
            logging.error(f"Error en cierre diario automático: {e}")

def tarea_cierre_semanal():
    if TELEGRAM_TOKEN:
        try:
            dias, totales = ventas_semana_sync()
            if totales:
                cnt, total, contado, credito, ganancia = totales
                msg = (
                    f"📊 <b>BALANCE AUTOMÁTICO SEMANAL (DOMINGO 8:00 PM)</b> 📊\n\n"
                    f"🔢 <b>Transacciones Semana:</b> {cnt}\n"
                    f"💵 <b>Total Contado:</b> <code>{formatear_cop(contado)}</code>\n"
                    f"💳 <b>Total Crédito:</b> <code>{formatear_cop(credito)}</code>\n"
                    f"💰 <b>Recaudación Total:</b> <code>{formatear_cop(total)}</code>\n"
                    f"📈 <b>Ganancia Neta Estimada:</b> <code>{formatear_cop(ganancia)}</code>\n\n"
                    f"📄 <i>Adjunto encontrarás el reporte PDF detallado de la semana.</i>"
                )
            else:
                msg = "📊 <b>BALANCE AUTOMÁTICO SEMANAL (DOMINGO 8:00 PM)</b> 📊\n\nℹ️ <i>No se registraron ventas durante esta semana.</i>"

            pdf_buffer = generar_pdf_semana_sync()
            now_co = datetime.now(COLOMBIA_TZ)
            admins = obtener_lista_admins()

            for admin_id in admins:
                enviar_mensaje_api(admin_id, msg)
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    enviar_documento_api(
                        admin_id, 
                        pdf_buffer, 
                        f"Reporte_Semanal_{now_co.strftime('%d_%m_%Y')}.pdf",
                        "📄 Reporte PDF Semanal"
                    )
        except Exception as e:
            logging.error(f"Error en cierre semanal automático: {e}")

def tarea_cierre_mensual_automatico():
    if TELEGRAM_TOKEN:
        try:
            pdf_buffer = generar_pdf_mes_sync(mes_offset=1)
            msg = "📈 <b>REPORTE AUTOMÁTICO MENSUAL</b> 📈\n\n📄 Adjunto encontrarás el PDF consolidado con todas las ventas del mes anterior."
            now_co = datetime.now(COLOMBIA_TZ)
            admins = obtener_lista_admins()

            for admin_id in admins:
                if pdf_buffer:
                    enviar_mensaje_api(admin_id, msg)
                    pdf_buffer.seek(0)
                    enviar_documento_api(
                        admin_id,
                        pdf_buffer,
                        f"Reporte_Mensual_Anterior_{now_co.strftime('%m_%Y')}.pdf",
                        "📄 Reporte PDF Mensual"
                    )
                else:
                    enviar_mensaje_api(admin_id, "📈 <b>REPORTE AUTOMÁTICO MENSUAL</b> 📈\n\nℹ️ <i>No se registraron ventas en el mes anterior.</i>")
        except Exception as e:
            logging.error(f"Error en reporte mensual automático: {e}")

# Inicialización con zona horaria oficial de Colombia
scheduler = BackgroundScheduler(timezone=COLOMBIA_TZ)

# Saludo de la mañana a las 7:00 AM
scheduler.add_job(tarea_saludo_manana, 'cron', hour=7, minute=0)

# Cierre Diario a las 7:00 PM (19:00 hrs)
scheduler.add_job(tarea_cierre_diario, 'cron', hour=19, minute=0)

# Cierre Semanal todos los domingos a las 8:00 PM (20:00 hrs)
scheduler.add_job(tarea_cierre_semanal, 'cron', day_of_week='sun', hour=20, minute=0)

# Cierre Mensual el día 1 de cada mes a las 8:00 AM
scheduler.add_job(tarea_cierre_mensual_automatico, 'cron', day=1, hour=8, minute=0)

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
            ptb_app.add_handler(CommandHandler("test_notificaciones", probar_notificaciones))
            
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
