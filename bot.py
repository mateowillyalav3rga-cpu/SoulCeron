import io
import json
import logging
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import psycopg2
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHAT_ID_ADMIN = os.getenv("CHAT_ID_ADMIN")

# IDs oficiales de administradores
ID_ESPOSA = "2059542689"
ID_ESPOSO = "5197161394"

# Control de desduplicación de mensajes por update_id
UPDATES_PROCESADOS = set()

# Estados para ConversationHandler
ESTADO_CANTIDAD_COSTO = 1
ESTADO_EDITAR_CANTIDAD = 2
ESTADO_AGREGAR_CANTIDAD = 3
ESTADO_CREAR_COMBO_DATOS = 4


def obtener_lista_admins():
    admins = [ID_ESPOSO, ID_ESPOSA]
    if CHAT_ID_ADMIN:
        for cid in CHAT_ID_ADMIN.split(","):
            cid_clean = cid.strip().replace('"', "").replace("'", "")
            if (
                cid_clean
                and cid_clean not in admins
                and cid_clean != "2074541555"
            ):
                admins.append(cid_clean)
    return admins


COLOMBIA_TZ = ZoneInfo("America/Bogota")

web_app = Flask(__name__)


def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


def enviar_mensaje_api(chat_id: str, texto: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": str(chat_id), "text": texto, "parse_mode": "HTML"}
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            logging.info(
                f"Envío API Telegram a {chat_id}: status {response.status}"
            )
    except urllib.error.HTTPError as e:
        error_resp = e.read().decode("utf-8")
        logging.error(f"Error HTTP {e.code} enviando a {chat_id}: {error_resp}")
    except Exception as e:
        logging.error(f"Error enviando mensaje API Telegram a {chat_id}: {e}")


def enviar_documento_api(
    chat_id: str, document_buffer, filename: str, caption: str = ""
):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"

    document_buffer.seek(0)
    file_bytes = document_buffer.read()

    body = bytearray()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode("utf-8")
    )
    body.extend(f"{chat_id}\r\n".encode("utf-8"))

    if caption:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode("utf-8")
        )
        body.extend(f"{caption}\r\n".encode("utf-8"))

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'.encode(
            "utf-8"
        )
    )
    body.extend("HTML\r\n".encode("utf-8"))

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(
            "utf-8"
        )
    )
    body.extend("Content-Type: application/pdf\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend("\r\n".encode("utf-8"))

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(
        url, data=bytes(body), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            logging.info(
                f"Envío Documento API Telegram a {chat_id}: status"
                f" {response.status}"
            )
    except urllib.error.HTTPError as e:
        error_resp = e.read().decode("utf-8")
        logging.error(f"Error HTTP Documento {e.code} a {chat_id}: {error_resp}")
    except Exception as e:
        logging.error(f"Error enviando documento API Telegram a {chat_id}: {e}")


def enviar_mensaje_seguro(texto: str, max_length: int = 4000) -> str:
    if len(texto) > max_length:
        return texto[:max_length] + "\n\n⚠️ <i>(Resultado recortado por longitud)</i>"
    return texto


def formatear_cop(monto):
    return f"${float(monto):,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


# -------------------------------------------------------------------
# MENÚ CON BOTONES INTERACTIVOS REORGANIZADO
# -------------------------------------------------------------------
def obtener_teclado_menu():
    keyboard = [
        [
            InlineKeyboardButton("🛍️ Registrar Venta", callback_data="btn_guia_venta"),
            InlineKeyboardButton("📦 Registrar Compra", callback_data="btn_menu_compras"),
        ],
        [
            InlineKeyboardButton("📊 Balances (Hoy / Sem. / Mes)", callback_data="btn_menu_balances"),
            InlineKeyboardButton("🏢 Valorización & Inventario", callback_data="btn_valorizacion"),
        ],
        [
            InlineKeyboardButton("💰 Costos & Recetas", callback_data="recetas_page_0"),
            InlineKeyboardButton("👥 Segmentación & Clientes", callback_data="btn_segmentacion"),
        ],
        [
            InlineKeyboardButton("⭐️ Productos & Combos", callback_data="btn_analisis_productos"),
            InlineKeyboardButton("🚫 Productos Agotados", callback_data="btn_stock_bajo"),
        ],
        [
            InlineKeyboardButton("❓ Ayuda / Formatos", callback_data="btn_ayuda"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def obtener_teclado_balances():
    keyboard = [
        [
            InlineKeyboardButton("📊 Ventas Hoy", callback_data="btn_ventas_hoy"),
            InlineKeyboardButton("📅 Balance Semanal", callback_data="btn_ventas_semana"),
        ],
        [
            InlineKeyboardButton("🗓️ Balance Mensual", callback_data="btn_ventas_mes"),
        ],
        [
            InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="btn_menu_principal"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = (
        f"✨ <b>¡Hola! Bienvenido al asistente general de Soulcerón</b> ✨\n\n"
        f"📱 <b>Tu ID de Chat:</b> <code>{chat_id}</code>\n\n"
        "Gestión unificada para <b>Repostería, Maquillaje, Empaques, Papelería y Kits/Combos</b>.\n\n"
        "👇 <i>Selecciona una opción del menú para comenzar:</i>"
    )
    if update.message:
        await update.message.reply_text(
            msg, reply_markup=obtener_teclado_menu(), parse_mode="HTML"
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            msg, reply_markup=obtener_teclado_menu(), parse_mode="HTML"
        )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🤖 <b>Formatos de Registro Rápidos y Menús</b>\n\n"
        "📝 <b>Registrar Venta (Comando <code>v</code> o <code>venta</code>):</b>\n"
        "<pre>\n"
        "v\n"
        "Juan Pérez\n"
        "contado\n"
        "Combo Chocolates x5, 1\n"
        "Labial Mágico, 2\n"
        "</pre>\n"
        "📌 <i>Línea 1: <code>v</code> | Línea 2: Cliente | Línea 3: <code>contado</code> o <code>credito</code> | Línea 4+: Productos</i>\n\n"
        "💄 <b>Registrar Compra Rápida (Sintaxis Exclusiva MAQUILLAJE):</b>\n"
        "<pre>\n"
        "c\n"
        "Pomo Limpiador, 50, 1300, 3000\n"
        "Labial Mágico, 20, 2500, 5000\n"
        "</pre>\n"
        "📌 <i>Línea 1: <code>c</code> | Línea 2+: Producto, Cantidad, Costo, PrecioVenta</i>\n\n"
        "📦 <b>Otras Compras (Repostería, Empaques, Papelería, Stickers):</b>\n"
        "Usa el botón <b>'📦 Registrar Compra'</b> en el menú principal para seleccionar la categoría interactivamente.\n\n"
        "🔍 <b>Consultar Cliente:</b> <code>n Juan Pérez</code>\n"
        "🔍 <b>Consultar Producto:</b> <code>p Labial</code>\n"
        "📱 <b>Abrir Menú Principal:</b> <code>m</code>"
    )
    if update.message:
        await update.message.reply_text(
            mensaje, reply_markup=obtener_teclado_menu(), parse_mode="HTML"
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            mensaje, reply_markup=obtener_teclado_menu(), parse_mode="HTML"
        )


# -------------------------------------------------------------------
# REGISTRO RÁPIDO DE COMPRAS (EXCLUSIVO MAQUILLAJE POR TEXTO - id_categoria = 1)
# -------------------------------------------------------------------
async def registrar_compra_maquillaje_rapida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]

    items_raw = []
    inicio_idx = 1 if lineas[0].lower() in ["c", "compra", "/compra", "/c"] else 0

    for l in lineas[inicio_idx:]:
        l_clean = re.sub(r"^\s*-\s*", "", l)
        parts = l_clean.split(",")
        if len(parts) == 4:
            prod_n, cant_s, costo_s, precio_s = [p.strip() for p in parts]
            if cant_s.isdigit():
                try:
                    items_raw.append((prod_n, int(cant_s), float(costo_s), float(precio_s)))
                except ValueError:
                    pass

    if not items_raw:
        await update.message.reply_text(
            "❌ <b>Formato de compra rápida (exclusivo Maquillaje) incorrecto.</b> Usa:\n\n"
            "<pre>\n"
            "c\n"
            "Producto Maquillaje, Cantidad, CostoCompra, PrecioVenta\n"
            "</pre>\n"
            "💡 <i>Para comprar insumos de Repostería, Empaques, Papelería o Stickers, usa el botón '📦 Registrar Compra' del menú.</i>",
            parse_mode="HTML",
        )
        return

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT id_categoria_insumo FROM categorias_insumos WHERE nombre = 'Maquillaje';")
        res_cat = cur.fetchone()
        id_cat_maq = res_cat[0] if res_cat else None

        resúmenes = []
        for prod_clean, cant, costo, precio in items_raw:
            query_update_p = """
                UPDATE productos 
                SET stock_actual = stock_actual + %s, costo_compra = %s, precio_venta = %s, id_categoria = 1
                WHERE LOWER(nombre) LIKE LOWER(%s)
                RETURNING id_producto, nombre, stock_actual;
            """
            cur.execute(query_update_p, (cant, costo, precio, f"%{prod_clean}%"))
            res_p = cur.fetchone()

            if not res_p:
                query_insert_p = """
                    INSERT INTO productos (nombre, stock_actual, costo_compra, precio_venta, id_categoria)
                    VALUES (%s, %s, %s, %s, 1)
                    RETURNING id_producto, nombre, stock_actual;
                """
                cur.execute(query_insert_p, (prod_clean, cant, costo, precio))
                res_p = cur.fetchone()

            id_prod, nom_prod, stock_p = res_p

            query_update_i = """
                UPDATE insumos 
                SET stock_actual = stock_actual + %s, costo_compra = %s, cantidad_comprada = %s, categoria_insumo = 'Maquillaje'
                WHERE LOWER(nombre) LIKE LOWER(%s)
                RETURNING id_insumo;
            """
            cur.execute(query_update_i, (cant, costo * cant, cant, f"%{prod_clean}%"))
            res_i = cur.fetchone()

            if not res_i:
                query_insert_i = """
                    INSERT INTO insumos (id_categoria_insumo, nombre, categoria_insumo, unidad_medida, costo_compra, cantidad_comprada, stock_actual)
                    VALUES (%s, %s, 'Maquillaje', 'unidades', %s, %s, %s)
                    RETURNING id_insumo;
                """
                cur.execute(query_insert_i, (id_cat_maq, nom_prod, costo * cant, cant, cant))
                res_i = cur.fetchone()

            id_ins = res_i[0]

            cur.execute("""
                INSERT INTO recetas (id_producto, id_insumo, cantidad_usada)
                VALUES (%s, %s, 1.0)
                ON CONFLICT (id_producto, id_insumo) DO UPDATE SET cantidad_usada = 1.0;
            """, (id_prod, id_ins))

            resúmenes.append(
                f"💄 <b>{nom_prod}</b> <code>[MAQUILLAJE]</code>\n"
                f" └ Stock actual: <code>{stock_p} uds.</code> | Costo U.: <code>{formatear_cop(costo)}</code> | Venta: <code>{formatear_cop(precio)}</code>"
            )

        conn.commit()
        cur.close()
        conn.close()

        msg = "💄 <b>Resumen de Compras de Maquillaje Registradas:</b>\n\n" + "\n\n".join(resúmenes)
        for admin_id in obtener_lista_admins():
            enviar_mensaje_api(admin_id, enviar_mensaje_seguro(msg))

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        await update.message.reply_text(f"🔴 <b>Error al registrar compra de maquillaje:</b> {str(e)}", parse_mode="HTML")


# -------------------------------------------------------------------
# REGISTRO DE COMPRAS INTERACTIVO DESDE MENÚ
# -------------------------------------------------------------------
async def mostrar_categorias_compras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id_categoria_insumo, nombre FROM categorias_insumos ORDER BY id_categoria_insumo ASC;")
    cats = cur.fetchall()
    cur.close()
    conn.close()

    keyboard = []
    row = []
    for cid, cnombre in cats:
        row.append(InlineKeyboardButton(f"📦 {cnombre}", callback_data=f"compra_cat_{cid}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="btn_menu_principal")])

    msg = "📦 <b>Registrar Compra / Reabastecimiento de Insumos</b>\n\nSelecciona la categoría del insumo que compraste:"
    await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def seleccionar_insumo_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat_id = int(query.data.replace("compra_cat_", ""))
    context.user_data["compra_cat_id"] = cat_id

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM categorias_insumos WHERE id_categoria_insumo = %s;", (cat_id,))
    cat_nombre = cur.fetchone()[0]

    cur.execute("SELECT id_insumo, nombre, unidad_medida FROM insumos WHERE id_categoria_insumo = %s ORDER BY nombre ASC;", (cat_id,))
    insumos = cur.fetchall()
    cur.close()
    conn.close()

    keyboard = []
    for i_id, i_nom, i_med in insumos:
        keyboard.append([InlineKeyboardButton(f"{i_nom} ({i_med})", callback_data=f"compra_ins_{i_id}")])

    keyboard.append([InlineKeyboardButton("➕ Registrar Insumo Nuevo en esta Categoría", callback_data=f"compra_nuevo_{cat_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Cambiar Categoría", callback_data="btn_menu_compras")])

    msg = f"📂 <b>Categoría: {cat_nombre}</b>\n\nSelecciona el insumo a reabastecer o crea uno nuevo:"
    await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def pedir_datos_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("compra_ins_"):
        ins_id = int(data.replace("compra_ins_", ""))
        context.user_data["compra_insumo_id"] = ins_id
        context.user_data["compra_es_nuevo"] = False

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT nombre, unidad_medida FROM insumos WHERE id_insumo = %s;", (ins_id,))
        nom, med = cur.fetchone()
        cur.close()
        conn.close()

        context.user_data["compra_insumo_nombre"] = nom
        context.user_data["compra_unidad_medida"] = med

        msg = (
            f"📦 <b>Reabastecer Insumo:</b> {nom}\n"
            f"📏 <b>Unidad de medida:</b> {med}\n\n"
            f"Escribe la <b>cantidad comprada</b> y el <b>costo total pagado</b> separados por coma o espacio:\n"
            f"<i>Ejemplo: <code>500, 25000</code> o <code>500 25000</code></i>"
        )
        await query.message.reply_text(msg, parse_mode="HTML")
        return ESTADO_CANTIDAD_COSTO

    elif data.startswith("compra_nuevo_"):
        cat_id = int(data.replace("compra_nuevo_", ""))
        context.user_data["compra_cat_id"] = cat_id
        context.user_data["compra_es_nuevo"] = True

        msg = (
            "✨ <b>Registrar Insumo Nuevo</b>\n\n"
            "Envía los datos en el siguiente formato:\n"
            "<code>Nombre Insumo, UnidadMedida (gramos/unidades/metros/ml), Cantidad, CostoTotal</code>\n\n"
            "<i>Ejemplo: <code>Bolsa Tela Rosa, unidades, 100, 35000</code></i>"
        )
        await query.message.reply_text(msg, parse_mode="HTML")
        return ESTADO_CANTIDAD_COSTO


async def guardar_compra_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    es_nuevo = context.user_data.get("compra_es_nuevo", False)

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        if es_nuevo:
            partes = [p.strip() for p in texto.split(",")]
            if len(partes) < 4:
                await update.message.reply_text("❌ Formato incorrecto. Usa: <code>Nombre, Unidad, Cantidad, CostoTotal</code>", parse_mode="HTML")
                cur.close()
                conn.close()
                return ESTADO_CANTIDAD_COSTO

            nom, med, cant_s, costo_s = partes[0], partes[1], partes[2], partes[3]
            cant = float(cant_s)
            costo_total = float(costo_s)
            cat_id = context.user_data["compra_cat_id"]

            cur.execute("SELECT nombre FROM categorias_insumos WHERE id_categoria_insumo = %s;", (cat_id,))
            cat_nom = cur.fetchone()[0]

            query_ins = """
                INSERT INTO insumos (id_categoria_insumo, nombre, categoria_insumo, unidad_medida, costo_compra, cantidad_comprada, stock_actual)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id_insumo;
            """
            cur.execute(query_ins, (cat_id, nom, cat_nom, med, costo_total, cant, cant))

            costo_u = costo_total / cant if cant > 0 else 0
            msg = (
                f"✨ <b>¡Nuevo Insumo Registrado con Éxito!</b>\n\n"
                f"📦 <b>Insumo:</b> {nom}\n"
                f"🏷️ <b>Categoría:</b> {cat_nom}\n"
                f"📊 <b>Cantidad:</b> {cant} {med}\n"
                f"💵 <b>Costo Total:</b> <code>{formatear_cop(costo_total)}</code>\n"
                f"📈 <b>Costo Unitario:</b> <code>{formatear_cop(costo_u)}</code> / {med}"
            )

        else:
            partes = re.split(r"[\s,]+", texto)
            if len(partes) < 2:
                await update.message.reply_text("❌ Envía la cantidad y el costo separados por espacio o coma.", parse_mode="HTML")
                cur.close()
                conn.close()
                return ESTADO_CANTIDAD_COSTO

            cant = float(partes[0])
            costo_total = float(partes[1])
            ins_id = context.user_data["compra_insumo_id"]
            nom = context.user_data["compra_insumo_nombre"]
            med = context.user_data["compra_unidad_medida"]

            query_upd = """
                UPDATE insumos 
                SET stock_actual = stock_actual + %s,
                    costo_compra = %s,
                    cantidad_comprada = %s
                WHERE id_insumo = %s
                RETURNING stock_actual;
            """
            cur.execute(query_upd, (cant, costo_total, cant, ins_id))
            nuevo_stock = cur.fetchone()[0]

            costo_u = costo_total / cant if cant > 0 else 0
            msg = (
                f"🟢 <b>Reabastecimiento Exitoso</b>\n\n"
                f"📦 <b>Insumo:</b> {nom}\n"
                f"➕ <b>Comprado:</b> +{cant} {med}\n"
                f"📊 <b>Nuevo Stock Total:</b> {nuevo_stock} {med}\n"
                f"📈 <b>Nuevo Costo U.:</b> <code>{formatear_cop(costo_u)}</code> / {med}"
            )

        conn.commit()
        cur.close()
        conn.close()

        for admin_id in obtener_lista_admins():
            enviar_mensaje_api(admin_id, enviar_mensaje_seguro(msg))

        await update.message.reply_text("👇 <i>Selecciona una opción del menú para continuar:</i>", reply_markup=obtener_teclado_menu(), parse_mode="HTML")
        return ConversationHandler.END

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        await update.message.reply_text(f"🔴 Error guardando compra: {str(e)}")
        return ConversationHandler.END


async def cancelar_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operación cancelada.", reply_markup=obtener_teclado_menu())
    return ConversationHandler.END


# -------------------------------------------------------------------
# EDITOR INTERACTIVO DE RECETAS / COMBOS CON PAGINACIÓN Y FILTRO (id_categoria != 1)
# -------------------------------------------------------------------
async def mostrar_costos_recetas_paginado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    page = int(data.replace("recetas_page_", "")) if data.startswith("recetas_page_") else 0

    conn = get_db_connection()
    cur = conn.cursor()

    # FILTRO ESTRICTO: Excluye todos los productos cuya id_categoria sea 1 (Maquillaje)
    cur.execute("""
        SELECT DISTINCT p.id_producto, p.nombre, p.precio_venta, COALESCE(v.costo_insumos_total, 0.0) as costo_insumos_total
        FROM productos p
        LEFT JOIN vista_costos_productos v ON p.id_producto = v.id_producto
        WHERE (p.id_categoria != 1 OR p.id_categoria IS NULL)
        ORDER BY p.nombre ASC;
    """)
    prods = cur.fetchall()
    cur.close()
    conn.close()

    items_por_pagina = 8
    total_items = len(prods)
    total_paginas = math.ceil(total_items / items_por_pagina) if total_items > 0 else 1

    start_idx = page * items_por_pagina
    end_idx = start_idx + items_por_pagina
    prods_page = prods[start_idx:end_idx]

    keyboard = [
        [InlineKeyboardButton("✨ Crear Nuevo Combo / Kit", callback_data="btn_crear_combo")]
    ]

    for pid, pnom, precio, costo_i in prods_page:
        c_i = float(costo_i) if costo_i is not None else 0.0
        keyboard.append([InlineKeyboardButton(f"📝 {pnom} (Costo: {formatear_cop(c_i)})", callback_data=f"ver_receta_{pid}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"recetas_page_{page - 1}"))
    if page < total_paginas - 1:
        nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"recetas_page_{page + 1}"))
    
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="btn_menu_principal")])

    msg = (
        f"💰 <b>Costos y Recetas / Combos & Kits</b> (Pág. {page + 1}/{total_paginas})\n\n"
        "Selecciona un producto o kit para ver/editar su receta, o crea uno nuevo:\n"
        "<i>(Excluidos productos de id_categoria = 1: Maquillaje)</i>"
    )

    try:
        await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def iniciar_crear_combo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    msg = (
        "✨ <b>Crear Nuevo Combo, Kit o Producto Compuesto</b>\n\n"
        "Envía los datos en el siguiente formato:\n"
        "<code>NombreCombo, PrecioVenta, NombreCategoria</code>\n\n"
        "📌 <i>Ejemplos:</i>\n"
        "• <code>Kit Navideño Especial, 85000, Kit Navideño</code>\n"
        "• <code>Combo Amor y Amistad #1, 45000, Kit Amor y Amistad</code>\n"
        "• <code>Caja Mini Donas x12, 32000, Mini Donitas</code>\n\n"
        "💡 <i>Si la categoría no existe en el sistema, se creará automáticamente en la tabla categorias.</i>"
    )
    await query.message.reply_text(msg, parse_mode="HTML")
    return ESTADO_CREAR_COMBO_DATOS


async def guardar_crear_combo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    partes = [p.strip() for p in texto.split(",")]

    if len(partes) < 3:
        await update.message.reply_text(
            "❌ Formato incorrecto. Debes enviar: <code>NombreCombo, PrecioVenta, Categoria</code>",
            parse_mode="HTML",
        )
        return ESTADO_CREAR_COMBO_DATOS

    nom_combo, precio_s, cat_nombre = partes[0], partes[1], partes[2]

    try:
        precio_v = float(precio_s)
    except ValueError:
        await update.message.reply_text("❌ El precio de venta debe ser un número válido.")
        return ESTADO_CREAR_COMBO_DATOS

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        # Verificar o crear categoría en la tabla 'categorias'
        cur.execute("SELECT id_categoria FROM categorias WHERE LOWER(nombre) = LOWER(%s);", (cat_nombre,))
        res_cat = cur.fetchone()
        if not res_cat:
            cur.execute("INSERT INTO categorias (nombre) VALUES (%s) RETURNING id_categoria;", (cat_nombre,))
            id_cat = cur.fetchone()[0]
        else:
            id_cat = res_cat[0]

        # Crear el producto asignando su id_categoria correspondiente
        cur.execute("""
            INSERT INTO productos (nombre, stock_actual, costo_compra, precio_venta, id_categoria)
            VALUES (%s, 0, 0.0, %s, %s)
            RETURNING id_producto;
        """, (nom_combo, precio_v, id_cat))
        id_prod = cur.fetchone()[0]

        conn.commit()
        cur.close()
        conn.close()

        msg = (
            f"🎉 <b>¡Nuevo Combo / Kit Creado Exitosamente!</b> 🎉\n\n"
            f"🎁 <b>Nombre:</b> {nom_combo}\n"
            f"🏷️ <b>Categoría:</b> {cat_nombre} (ID: {id_cat})\n"
            f"🏷️ <b>Precio de Venta:</b> <code>{formatear_cop(precio_v)}</code>\n\n"
            f"💡 <i>Ahora puedes seleccionarlo en 'Costos & Recetas' para agregarle sus empaques o insumos.</i>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

        keyboard = [[InlineKeyboardButton("📝 Configurar Receta de este Kit", callback_data=f"ver_receta_{id_prod}")]]
        await update.message.reply_text("👇 Selecciona para agregar sus componentes:", reply_markup=InlineKeyboardMarkup(keyboard))

        return ConversationHandler.END

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        await update.message.reply_text(f"🔴 Error al crear el combo: {str(e)}")
        return ConversationHandler.END


async def ver_detalle_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pid = int(query.data.replace("ver_receta_", ""))
    context.user_data["receta_producto_id"] = pid

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT nombre, precio_venta FROM productos WHERE id_producto = %s;", (pid,))
    pnom, precio = cur.fetchone()

    cur.execute("""
        SELECT r.id_insumo, i.nombre, r.cantidad_usada, i.unidad_medida, i.costo_unitario, (r.cantidad_usada * i.costo_unitario) as costo_parcial
        FROM recetas r
        JOIN insumos i ON r.id_insumo = i.id_insumo
        WHERE r.id_producto = %s
        ORDER BY i.nombre ASC;
    """, (pid,))
    insumos_receta = cur.fetchall()
    cur.close()
    conn.close()

    costo_total = sum(float(row[5]) for row in insumos_receta) if insumos_receta else 0.0
    precio_fl = float(precio)
    ganancia = precio_fl - costo_total

    lineas = [f"🍫 <b>Producto / Kit / Combo:</b> {pnom}\n" + "─"*30]
    if not insumos_receta:
        lineas.append("⚠️ <i>Este producto aún no tiene insumos, artículos o empaques asociados.</i>")
    else:
        for iid, inom, cant, med, c_u, c_parcial in insumos_receta:
            lineas.append(f"• <b>{inom}:</b> {float(cant):,.2f} {med} (<code>{formatear_cop(c_parcial)}</code>)")

    lineas.append("─"*30)
    lineas.append(f"💵 <b>Costo Total Ensamble:</b> <code>{formatear_cop(costo_total)}</code>")
    lineas.append(f"🏷️ <b>Precio de Venta:</b> <code>{formatear_cop(precio_fl)}</code>")
    lineas.append(f"📈 <b>Ganancia Estimada:</b> <code>{formatear_cop(ganancia)}</code>")

    keyboard = [
        [InlineKeyboardButton("➕ Agregar Insumo / Producto a este Kit", callback_data=f"rec_add_page_{pid}_0")],
        [
            InlineKeyboardButton("✏️ Modificar Cantidad / Peso", callback_data=f"rec_mod_{pid}"),
            InlineKeyboardButton("🗑️ Quitar Insumo", callback_data=f"rec_del_{pid}"),
        ],
        [InlineKeyboardButton("🔙 Volver a Lista de Recetas", callback_data="recetas_page_0")],
    ]

    await query.message.reply_text("\n".join(lineas), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def opciones_quitar_insumo_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pid = int(query.data.replace("rec_del_", ""))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id_insumo, i.nombre 
        FROM recetas r 
        JOIN insumos i ON r.id_insumo = i.id_insumo 
        WHERE r.id_producto = %s;
    """, (pid,))
    items = cur.fetchall()
    cur.close()
    conn.close()

    if not items:
        await query.message.reply_text("❌ No hay insumos en esta receta para eliminar.")
        return

    keyboard = []
    for iid, inom in items:
        keyboard.append([InlineKeyboardButton(f"🗑️ Quitar {inom}", callback_data=f"confirm_del_rec_{pid}_{iid}")])
    keyboard.append([InlineKeyboardButton("🔙 Volver a la Receta", callback_data=f"ver_receta_{pid}")])

    await query.message.reply_text("Selecciona el componente que deseas retirar de la receta/kit:", reply_markup=InlineKeyboardMarkup(keyboard))


async def ejecutar_quitar_insumo_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    pid, iid = int(parts[3]), int(parts[4])

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recetas WHERE id_producto = %s AND id_insumo = %s;", (pid, iid))
    conn.commit()
    cur.close()
    conn.close()

    await query.message.reply_text("✅ Insumo/Producto eliminado de la receta exitosamente.")
    query.data = f"ver_receta_{pid}"
    await ver_detalle_receta(update, context)


async def opciones_modificar_cantidad_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pid = int(query.data.replace("rec_mod_", ""))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id_insumo, i.nombre, r.cantidad_usada, i.unidad_medida 
        FROM recetas r 
        JOIN insumos i ON r.id_insumo = i.id_insumo 
        WHERE r.id_producto = %s;
    """, (pid,))
    items = cur.fetchall()
    cur.close()
    conn.close()

    if not items:
        await query.message.reply_text("❌ No hay insumos en esta receta para modificar.")
        return

    keyboard = []
    for iid, inom, cant, med in items:
        keyboard.append([InlineKeyboardButton(f"✏️ {inom} (Actual: {float(cant)} {med})", callback_data=f"pedir_mod_cant_{pid}_{iid}")])
    keyboard.append([InlineKeyboardButton("🔙 Volver a la Receta", callback_data=f"ver_receta_{pid}")])

    await query.message.reply_text("Selecciona el insumo o producto al que deseas cambiarle la cantidad/peso:", reply_markup=InlineKeyboardMarkup(keyboard))


async def pedir_nueva_cantidad_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    pid, iid = int(parts[3]), int(parts[4])
    context.user_data["edit_rec_pid"] = pid
    context.user_data["edit_rec_iid"] = iid

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nombre, unidad_medida FROM insumos WHERE id_insumo = %s;", (iid,))
    inom, med = cur.fetchone()
    cur.close()
    conn.close()

    msg = f"✏️ <b>Modificar Cantidad/Peso</b>\n\nInsumo: <b>{inom}</b>\nUnidad: {med}\n\nEscribe el nuevo valor a utilizar:"
    await query.message.reply_text(msg, parse_mode="HTML")
    return ESTADO_EDITAR_CANTIDAD


async def guardar_nueva_cantidad_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        nueva_cant = float(texto)
        pid = context.user_data["edit_rec_pid"]
        iid = context.user_data["edit_rec_iid"]

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE recetas SET cantidad_usada = %s WHERE id_producto = %s AND id_insumo = %s;", (nueva_cant, pid, iid))
        conn.commit()
        cur.close()
        conn.close()

        await update.message.reply_text("✅ Cantidad actualizada con éxito.")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Ingresa un número válido para la cantidad.")
        return ESTADO_EDITAR_CANTIDAD


async def opciones_agregar_insumo_receta_paginado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    pid = int(parts[3])
    page = int(parts[4])
    context.user_data["edit_rec_pid"] = pid

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id_insumo, nombre, unidad_medida, categoria_insumo FROM insumos ORDER BY categoria_insumo ASC, nombre ASC;")
    insumos = cur.fetchall()
    cur.close()
    conn.close()

    items_por_pagina = 8
    total_items = len(insumos)
    total_paginas = math.ceil(total_items / items_por_pagina) if total_items > 0 else 1

    start_idx = page * items_por_pagina
    end_idx = start_idx + items_por_pagina
    insumos_page = insumos[start_idx:end_idx]

    keyboard = []
    for iid, inom, med, cat in insumos_page:
        keyboard.append([InlineKeyboardButton(f"➕ [{cat}] {inom} ({med})", callback_data=f"sel_add_ins_{pid}_{iid}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"rec_add_page_{pid}_{page - 1}"))
    if page < total_paginas - 1:
        nav_row.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"rec_add_page_{pid}_{page + 1}"))

    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔙 Volver a la Receta", callback_data=f"ver_receta_{pid}")])

    msg = f"Selecciona el insumo o producto que deseas agregar a este kit/combo (Pág. {page + 1}/{total_paginas}):"
    
    try:
        await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


async def pedir_cantidad_agregar_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    pid, iid = int(parts[3]), int(parts[4])
    context.user_data["edit_rec_pid"] = pid
    context.user_data["edit_rec_iid"] = iid

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nombre, unidad_medida FROM insumos WHERE id_insumo = %s;", (iid,))
    inom, med = cur.fetchone()
    cur.close()
    conn.close()

    msg = f"➕ <b>Agregar a la Receta:</b> {inom}\n📏 <b>Unidad:</b> {med}\n\nEscribe la cantidad que consume este producto/kit:"
    await query.message.reply_text(msg, parse_mode="HTML")
    return ESTADO_AGREGAR_CANTIDAD


async def guardar_agregar_insumo_receta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        cant_usada = float(texto)
        pid = context.user_data["edit_rec_pid"]
        iid = context.user_data["edit_rec_iid"]

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO recetas (id_producto, id_insumo, cantidad_usada)
            VALUES (%s, %s, %s)
            ON CONFLICT (id_producto, id_insumo) DO UPDATE SET cantidad_usada = EXCLUDED.cantidad_usada;
        """, (pid, iid, cant_usada))
        conn.commit()
        cur.close()
        conn.close()

        await update.message.reply_text("✅ Componente agregado a la receta exitosamente.")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Ingresa un número válido.")
        return ESTADO_AGREGAR_CANTIDAD


# -------------------------------------------------------------------
# LÓGICA DE REGISTRO DE VENTA
# -------------------------------------------------------------------
async def registrar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]

    if len(lineas) < 4:
        await update.message.reply_text(
            "❌ <b>Formato de venta incorrecto.</b> Usa:\n\n"
            "<pre>\n"
            "v\n"
            "Nombre Cliente\n"
            "contado\n"
            "Producto, Cantidad\n"
            "Otro Producto, 2\n"
            "</pre>",
            parse_mode="HTML",
        )
        return

    cliente_nombre = lineas[1]
    tipo_pago = lineas[2].lower()

    if tipo_pago not in ["contado", "credito"]:
        await update.message.reply_text(
            "❌ El tipo de pago (línea 3) debe ser <code>contado</code> o"
            " <code>credito</code>.",
            parse_mode="HTML",
        )
        return

    items_raw = []
    for l in lineas[3:]:
        l_clean = re.sub(r"^\s*-\s*", "", l)
        parts = l_clean.split(",")
        if len(parts) >= 2:
            prod_p = ",".join(parts[:-1]).strip()
            cant_p = parts[-1].strip()
            if cant_p.isdigit() and int(cant_p) > 0:
                items_raw.append((prod_p, int(cant_p)))

    if not items_raw:
        await update.message.reply_text(
            "❌ No se reconocieron productos válidos. Revisa el formato (Nombre,"
            " Cantidad).",
            parse_mode="HTML",
        )
        return

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        productos_encontrados = []
        productos_no_encontrados = []

        for prod_clean, cantidad_deseada in items_raw:
            query_check = """
                SELECT id_producto, nombre, precio_venta, stock_actual
                FROM productos 
                WHERE LOWER(nombre) LIKE LOWER(%s) 
                ORDER BY 
                    CASE WHEN LOWER(nombre) = LOWER(%s) THEN 1 ELSE 2 END,
                    LENGTH(nombre) ASC 
                LIMIT 1 FOR UPDATE;
            """
            cur.execute(query_check, (f"%{prod_clean}%", prod_clean))
            prod_res = cur.fetchone()

            if prod_res:
                p_id, p_nombre, p_precio, p_stock_actual = prod_res
                productos_encontrados.append({
                    "id_producto": p_id,
                    "nombre_real": p_nombre,
                    "cantidad_vendida": cantidad_deseada,
                    "precio_unitario": float(p_precio),
                    "subtotal": cantidad_deseada * float(p_precio),
                    "stock_inicial": int(p_stock_actual),
                })
            else:
                productos_no_encontrados.append(prod_clean)

        if productos_no_encontrados:
            cur.close()
            conn.close()
            lineas_error = [
                "🔴 <b>Venta cancelada. Los siguientes productos no existen en inventario:</b>\n"
            ]
            for p_err in productos_no_encontrados:
                lineas_error.append(f"❌ <code>{p_err}</code>")
            lineas_error.append("\n💡 <i>Regístralos primero con <code>c</code> o mediante el menú antes de venderlos.</i>")
            await update.message.reply_text("\n".join(lineas_error), parse_mode="HTML")
            return

        query_check_cliente = (
            "SELECT id_cliente FROM clientes WHERE LOWER(nombre) LIKE LOWER(%s) LIMIT 1;"
        )
        cur.execute(query_check_cliente, (f"%{cliente_nombre}%",))
        res_c = cur.fetchone()

        if res_c:
            id_cliente = res_c[0]
            etiqueta_cliente = "👤 <code>[CLIENTE REGISTRADO]</code>"
        else:
            query_add_cliente = (
                "INSERT INTO clientes (nombre) VALUES (%s) RETURNING id_cliente;"
            )
            cur.execute(query_add_cliente, (cliente_nombre,))
            id_cliente = cur.fetchone()[0]
            etiqueta_cliente = "✨ <code>[NUEVO CLIENTE]</code>"

        query_nueva_venta = (
            "INSERT INTO ventas (id_cliente, tipo_pago) VALUES (%s, %s) RETURNING id_venta;"
        )
        cur.execute(query_nueva_venta, (id_cliente, tipo_pago))
        id_venta = cur.fetchone()[0]

        total_venta = 0.0
        lista_detalles_msg = []

        for item in productos_encontrados:
            query_detalle = """
                INSERT INTO detalle_ventas (id_venta, id_producto, cantidad, precio_unitario)
                VALUES (%s, %s, %s, %s);
            """
            cur.execute(
                query_detalle,
                (
                    id_venta,
                    item["id_producto"],
                    item["cantidad_vendida"],
                    item["precio_unitario"],
                ),
            )

            stock_existente = item["stock_inicial"]
            unidades_a_restar = item["cantidad_vendida"]
            nuevo_stock_calculado = max(0, stock_existente - unidades_a_restar)

            query_actualizar_stock = """
                UPDATE productos 
                SET stock_actual = %s
                WHERE id_producto = %s;
            """
            cur.execute(query_actualizar_stock, (nuevo_stock_calculado, item["id_producto"]))

            cur.execute("""
                UPDATE insumos 
                SET stock_actual = GREATEST(0, stock_actual - (%s * r.cantidad_usada))
                FROM recetas r
                WHERE r.id_insumo = insumos.id_insumo AND r.id_producto = %s;
            """, (unidades_a_restar, item["id_producto"]))

            total_venta += item["subtotal"]
            lista_detalles_msg.append(
                f"• <b>{item['nombre_real']}</b> x{unidades_a_restar} — "
                f"<code>{formatear_cop(item['subtotal'])}</code> (<i>Habían: {stock_existente} | Quedan: {nuevo_stock_calculado} uds.</i>)"
            )

        conn.commit()
        cur.close()
        conn.close()

        msg = (
            f"🎉 <b>¡Venta #{id_venta} Registrada Exitosamente!</b> 🎉\n\n"
            f"👤 <b>Cliente:</b> {cliente_nombre} {etiqueta_cliente}\n"
            f"💳 <b>Método de Pago:</b> {tipo_pago.capitalize()}\n\n"
            "📋 <b>Productos Procesados:</b>\n"
            + "\n".join(lista_detalles_msg)
            + "\n\n"
            f"💰 <b>TOTAL COBRADO:</b> <code>{formatear_cop(total_venta)}</code>"
        )

        admins = obtener_lista_admins()
        for admin_id in admins:
            enviar_mensaje_api(admin_id, enviar_mensaje_seguro(msg))

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        logging.error(f"Error registrando venta: {e}")
        await update.message.reply_text(f"🔴 <b>Error al registrar venta:</b> {str(e)}", parse_mode="HTML")


# -------------------------------------------------------------------
# LÓGICA DE BÚSQUEDA Y CONSULTAS
# -------------------------------------------------------------------
async def consultar_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    partes = texto.split(maxsplit=1)

    if len(partes) < 2:
        await update.message.reply_text(
            "❌ <b>Indica el nombre del producto.</b> Usa:\n<code>p Pomo</code> o <code>producto Labial</code>",
            parse_mode="HTML",
        )
        return

    nombre_buscar = partes[1].strip()

    query = """
        SELECT p.nombre, p.stock_actual, p.costo_compra, p.precio_venta, COALESCE(pr.nombre_empresa, 'Sin Proveedor')
        FROM productos p
        LEFT JOIN proveedores pr ON p.id_proveedor = pr.id_proveedor
        WHERE LOWER(p.nombre) LIKE LOWER(%s)
        ORDER BY LENGTH(p.nombre) ASC
        LIMIT 5;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (f"%{nombre_buscar}%",))
        resultados = cur.fetchall()
        cur.close()
        conn.close()

        if not resultados:
            await update.message.reply_text(
                f"🔍 No se encontraron productos coincidentes con <code>{nombre_buscar}</code>.",
                parse_mode="HTML",
            )
            return

        lineas = [f"🔍 <b>Resultados para '<code>{nombre_buscar}</code>':</b>\n"]
        for p_nom, stock, costo, precio, prov in resultados:
            lineas.append(
                f"📦 <b>{p_nom}</b>\n"
                f"   └ Stock: <code>{stock} uds.</code> | Costo: <code>{formatear_cop(costo)}</code> | Venta: <code>{formatear_cop(precio)}</code>\n"
                f"   └ Proveedor: <i>{prov}</i>\n"
            )

        await update.message.reply_text(
            "\n".join(lineas),
            reply_markup=obtener_teclado_menu(),
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"🔴 <b>Error al consultar producto:</b> {str(e)}", parse_mode="HTML")


# -------------------------------------------------------------------
# OTRAS FUNCIONES Y MÉTRICAS
# -------------------------------------------------------------------
def ventas_hoy_sync():
    query = """
        SELECT 
            COUNT(DISTINCT v.id_venta) AS total_ventas,
            COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_contado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_credito,
            COALESCE(SUM(dv.cantidad * p.costo_compra), 0) AS total_inversion,
            COALESCE(SUM(CASE WHEN dv.precio_unitario - p.costo_compra > 0 THEN dv.cantidad * (dv.precio_unitario - p.costo_compra) ELSE 0 END), 0) AS total_ganancia
        FROM ventas v
        JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
        JOIN productos p ON dv.id_producto = p.id_producto
        WHERE DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE(CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota');
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    res = cur.fetchone()
    cur.close()
    conn.close()

    cnt = int(res[0])
    recaudado = float(res[1])
    contado = float(res[2])
    credito = float(res[3])
    inversion = float(res[4])
    ganancia = float(res[5])

    return {
        "ventas": cnt,
        "recaudado": recaudado,
        "contado": contado,
        "credito": credito,
        "inversion": inversion,
        "ganancia": ganancia,
        "ahorro_50": ganancia * 0.5,
    }


def ventas_semana_sync(semana_offset=0):
    query_dias = f"""
        SELECT 
            DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') AS fecha,
            TO_CHAR(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota', 'TMDay') AS dia_nombre,
            COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_dia,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS contado_dia,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS credito_dia
        FROM ventas v
        JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
        WHERE DATE_TRUNC('week', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('week', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota' - INTERVAL '{semana_offset} week')
        GROUP BY DATE(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota'), TO_CHAR(v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota', 'TMDay')
        ORDER BY fecha ASC;
    """

    query_totales = f"""
        SELECT 
            COUNT(DISTINCT v.id_venta) AS total_ventas,
            COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_contado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_credito,
            COALESCE(SUM(dv.cantidad * p.costo_compra), 0) AS total_inversion,
            COALESCE(SUM(CASE WHEN dv.precio_unitario - p.costo_compra > 0 THEN dv.cantidad * (dv.precio_unitario - p.costo_compra) ELSE 0 END), 0) AS ganancia_total
        FROM ventas v
        JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
        JOIN productos p ON dv.id_producto = p.id_producto
        WHERE DATE_TRUNC('week', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('week', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota' - INTERVAL '{semana_offset} week');
    """

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query_dias)
    dias = cur.fetchall()

    cur.execute(query_totales)
    res = cur.fetchone()

    cur.close()
    conn.close()

    cnt = int(res[0])
    recaudado = float(res[1])
    contado = float(res[2])
    credito = float(res[3])
    inversion = float(res[4])
    ganancia = float(res[5])

    totales_dict = {
        "ventas": cnt,
        "recaudado": recaudado,
        "contado": contado,
        "credito": credito,
        "inversion": inversion,
        "ganancia": ganancia,
        "ahorro_50": ganancia * 0.5,
    }

    return dias, totales_dict


def ventas_mes_sync(mes_offset=0):
    query_totales = f"""
        SELECT 
            COUNT(DISTINCT v.id_venta) AS total_ventas,
            COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'contado' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_contado,
            COALESCE(SUM(CASE WHEN v.tipo_pago = 'credito' THEN dv.cantidad * dv.precio_unitario ELSE 0 END), 0) AS total_credito,
            COALESCE(SUM(dv.cantidad * p.costo_compra), 0) AS total_inversion,
            COALESCE(SUM(CASE WHEN dv.precio_unitario - p.costo_compra > 0 THEN dv.cantidad * (dv.precio_unitario - p.costo_compra) ELSE 0 END), 0) AS ganancia_total
        FROM ventas v
        JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
        JOIN productos p ON dv.id_producto = p.id_producto
        WHERE DATE_TRUNC('month', v.fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Bogota') = DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota' - INTERVAL '{mes_offset} month');
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query_totales)
    res = cur.fetchone()
    cur.close()
    conn.close()

    cnt = int(res[0])
    recaudado = float(res[1])
    contado = float(res[2])
    credito = float(res[3])
    inversion = float(res[4])
    ganancia = float(res[5])

    return {
        "ventas": cnt,
        "recaudado": recaudado,
        "contado": contado,
        "credito": credito,
        "inversion": inversion,
        "ganancia": ganancia,
        "ahorro_50": ganancia * 0.5,
    }


def construir_mensaje_resumen_completo(titulo, datos):
    if datos["ventas"] == 0:
        return f"ℹ️ <b>No se registraron ventas en este periodo ({titulo}).</b>"

    return (
        f"📊 <b>{titulo}</b>\n\n"
        f"🔢 <b>Total Ventas Realizadas:</b> {datos['ventas']}\n"
        f"💰 <b>Total Recaudado:</b> <code>{formatear_cop(datos['recaudado'])}</code>\n"
        f"💵 <b>Total Contado:</b> <code>{formatear_cop(datos['contado'])}</code>\n"
        f"💳 <b>Total Crédito:</b> <code>{formatear_cop(datos['credito'])}</code>\n"
        f"📦 <b>Total Inversión (Costo Producto):</b> <code>{formatear_cop(datos['inversion'])}</code>\n"
        f"📈 <b>Total Ganancia Neta:</b> <code>{formatear_cop(datos['ganancia'])}</code>\n"
        f"🏦 <b>50% Ganancias para Ahorro:</b> <code>{formatear_cop(datos['ahorro_50'])}</code>"
    )


def construir_comparativa_semanal():
    _, actual = ventas_semana_sync(0)
    _, anterior = ventas_semana_sync(1)

    def diff_str(val_act, val_ant):
        dif = val_act - val_ant
        if val_ant > 0:
            pct = (dif / val_ant) * 100
            signo = "+" if dif >= 0 else ""
            return (
                f"<code>{formatear_cop(val_act)}</code> ({signo}{pct:.1f}% vs."
                f" semana ant. <code>{formatear_cop(val_ant)}</code>)"
            )
        else:
            return f"<code>{formatear_cop(val_act)}</code> (Sin datos previos)"

    msg = (
        f"📉 <b>COMPARATIVA SEMANAL (Vs. Semana Anterior)</b> 📈\n\n"
        f"🔢 <b>Ventas:</b> {actual['ventas']} (Sem. Anterior: {anterior['ventas']})\n"
        f"💰 <b>Recaudado:</b> {diff_str(actual['recaudado'], anterior['recaudado'])}\n"
        f"💵 <b>Contado:</b> {diff_str(actual['contado'], anterior['contado'])}\n"
        f"💳 <b>Crédito:</b> {diff_str(actual['credito'], anterior['credito'])}\n"
        f"📦 <b>Inversión:</b> {diff_str(actual['inversion'], anterior['inversion'])}\n"
        f"📈 <b>Ganancia Neta:</b> {diff_str(actual['ganancia'], anterior['ganancia'])}\n"
        f"🏦 <b>Ahorro (50%):</b> {diff_str(actual['ahorro_50'], anterior['ahorro_50'])}"
    )
    return msg


def construir_comparativa_mensual():
    actual = ventas_mes_sync(1)
    anterior = ventas_mes_sync(2)

    def diff_str(val_act, val_ant):
        dif = val_act - val_ant
        if val_ant > 0:
            pct = (dif / val_ant) * 100
            signo = "+" if dif >= 0 else ""
            return (
                f"<code>{formatear_cop(val_act)}</code> ({signo}{pct:.1f}% vs. mes ant. <code>{formatear_cop(val_ant)}</code>)"
            )
        else:
            return f"<code>{formatear_cop(val_act)}</code> (Sin datos previos)"

    msg = (
        f"📉 <b>COMPARATIVA MENSUAL (Mes Cerrado Vs. Mes Anterior)</b> 📈\n\n"
        f"🔢 <b>Ventas:</b> {actual['ventas']} (Mes Antepasado: {anterior['ventas']})\n"
        f"💰 <b>Recaudado:</b> {diff_str(actual['recaudado'], anterior['recaudado'])}\n"
        f"💵 <b>Contado:</b> {diff_str(actual['contado'], anterior['contado'])}\n"
        f"💳 <b>Crédito:</b> {diff_str(actual['credito'], anterior['credito'])}\n"
        f"📦 <b>Inversión:</b> {diff_str(actual['inversion'], anterior['inversion'])}\n"
        f"📈 <b>Ganancia Neta:</b> {diff_str(actual['ganancia'], anterior['ganancia'])}\n"
        f"🏦 <b>Ahorro (50%):</b> {diff_str(actual['ahorro_50'], anterior['ahorro_50'])}"
    )
    return msg


def stock_bajo_sync():
    query = """
        SELECT p.nombre, p.stock_actual, p.costo_compra::float, p.precio_venta::float, COALESCE(pr.nombre_empresa, 'Sin Proveedor') AS proveedor
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
    c = float(res[0]) if res[0] is not None else 0.0
    v = float(res[1]) if res[1] is not None else 0.0
    return c, v


def insumos_categorizados_sync():
    query = """
        SELECT i.nombre, i.stock_actual, i.unidad_medida, i.costo_unitario, (i.stock_actual * i.costo_unitario) as costo_total_insumo, COALESCE(c.nombre, 'Sin Categoría') as categoria
        FROM insumos i
        LEFT JOIN categorias_insumos c ON i.id_categoria_insumo = c.id_categoria_insumo
        ORDER BY c.nombre ASC, i.nombre ASC;
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas


async def consultar_cliente(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    args = context.args
    if not args:
        partes = update.message.text.strip().split(maxsplit=1)
        if len(partes) > 1:
            nombre_buscar = partes[1].strip()
        else:
            await update.message.reply_text(
                "❌ <b>Indica el nombre del cliente.</b> Usa:\n<code>n Nombre</code> o <code>nombre Nombre</code>",
                parse_mode="HTML",
            )
            return
    else:
        nombre_buscar = " ".join(args).strip()

    query = """
        SELECT 
            nombre,
            total_compras,
            total_contado,
            total_credito,
            categoria_calculada,
            ultima_compra,
            dias_sin_comprar
        FROM vista_clientes_segmentados
        WHERE LOWER(nombre) LIKE LOWER(%s)
        LIMIT 1;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (f"%{nombre_buscar}%",))
        res = cur.fetchone()
        cur.close()
        conn.close()

        if not res or res[0] is None:
            await update.message.reply_text(
                f"🔍 <b>No se encontró ningún cliente registrado con el nombre</b> <code>{nombre_buscar}</code>.",
                parse_mode="HTML",
            )
            return

        nombre_cl, compras, contado, credito, categoria, ult_compra, dias_sin = res
        ult_compra_str = (
            ult_compra.strftime("%d/%m/%Y") if ult_compra else "Sin compras previas"
        )
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
        await update.message.reply_text(
            enviar_mensaje_seguro(msg),
            reply_markup=obtener_teclado_menu(),
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"🔴 <b>Error al consultar cliente:</b> {str(e)}", parse_mode="HTML"
        )


# -------------------------------------------------------------------
# GENERADORES PDF Y REPORTES
# -------------------------------------------------------------------
async def probar_notificaciones(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    admins = obtener_lista_admins()
    await update.message.reply_text(
        "🧪 <b>Iniciando prueba de notificaciones automáticas...</b>\nDestinatarios:"
        f" <code>{admins}</code>",
        parse_mode="HTML",
    )
    tarea_saludo_manana()
    tarea_cierre_diario()
    tarea_cierre_semanal()
    tarea_cierre_mensual_automatico()
    await update.message.reply_text(
        "✅ <b>Prueba ejecutada exitosamente.</b> Revisa los chats.",
        parse_mode="HTML",
    )


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
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, textColor=colors.HexColor("#880E4F"), spaceAfter=8
    )
    sub_style = ParagraphStyle(
        "SubStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, textColor=colors.gray, spaceAfter=15
    )

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte Diario de Ventas", titulo_style),
        Paragraph(f"Cierre de Día - Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style),
    ]

    tabla_data = [["ID", "Hora", "Cliente", "Pago", "Producto", "Cant", "Subtotal"]]
    grand_total = 0.0

    for id_v, fecha, cl, pago, prod, cant, precio, subtotal in detalles:
        sub_fl = float(subtotal)
        grand_total += sub_fl
        tabla_data.append([
            str(id_v),
            fecha.strftime("%H:%M"),
            str(cl),
            str(pago).capitalize(),
            str(prod),
            str(cant),
            formatear_cop(sub_fl),
        ])

    tabla_data.append(["", "", "", "", "", "TOTAL:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[35, 55, 120, 60, 150, 35, 85])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8BBD0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#880E4F")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F5F5F5")),
        ])
    )

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
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=18, textColor=colors.HexColor("#D81B60"), spaceAfter=12
    )
    sub_style = ParagraphStyle(
        "SubStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=10, textColor=colors.gray, spaceAfter=20
    )

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte Mensual de Ventas", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style),
    ]

    tabla_data = [["ID Venta", "Fecha", "Cliente", "Tipo Pago", "Total"]]
    grand_total = 0.0

    for id_v, fecha, cl, pago, total in ventas:
        tot_fl = float(total)
        grand_total += tot_fl
        tabla_data.append([
            str(id_v),
            fecha.strftime("%Y-%m-%d"),
            str(cl),
            str(pago).capitalize(),
            formatear_cop(tot_fl),
        ])

    tabla_data.append(["", "", "", "TOTAL MES:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[60, 80, 180, 80, 100])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8BBD0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#880E4F")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F5F5F5")),
        ])
    )

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
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, textColor=colors.HexColor("#880E4F"), spaceAfter=8
    )
    sub_style = ParagraphStyle(
        "SubStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, textColor=colors.gray, spaceAfter=15
    )

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Balance Semanal Detallado", titulo_style),
        Paragraph(f"Semana en curso - Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style),
    ]

    tabla_data = [["ID", "Fecha", "Cliente", "Pago", "Producto", "Cant", "Subtotal"]]
    grand_total = 0.0

    for id_v, fecha, cl, pago, prod, cant, precio, subtotal in detalles:
        sub_fl = float(subtotal)
        grand_total += sub_fl
        tabla_data.append([
            str(id_v),
            fecha.strftime("%d/%m"),
            str(cl),
            str(pago).capitalize(),
            str(prod),
            str(cant),
            formatear_cop(sub_fl),
        ])

    tabla_data.append(["", "", "", "", "", "TOTAL:", formatear_cop(grand_total)])

    t = Table(tabla_data, colWidths=[35, 55, 120, 60, 150, 35, 85])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8BBD0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#880E4F")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F5F5F5")),
        ])
    )

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer


def generar_pdf_valorizacion_y_categorizado_sync():
    insumos = insumos_categorizados_sync()
    if not insumos:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, textColor=colors.HexColor("#880E4F"), spaceAfter=8
    )
    sub_style = ParagraphStyle(
        "SubStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, textColor=colors.gray, spaceAfter=15
    )
    cat_header_style = ParagraphStyle(
        "CatHeaderStyle", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11, textColor=colors.HexColor("#AD1457"), spaceBefore=10, spaceAfter=5
    )

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Inventario Categorizado y Valorización de Insumos", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style),
    ]

    categorizados = {}
    total_general_costo = 0.0

    for inom, stock, med, c_u, c_tot, cat_nombre in insumos:
        stock_fl = float(stock)
        c_u_fl = float(c_u) if c_u else 0.0
        c_tot_fl = float(c_tot) if c_tot else (stock_fl * c_u_fl)
        total_general_costo += c_tot_fl

        categorizados.setdefault(cat_nombre, []).append([
            str(inom),
            f"{stock_fl:,.2f} {med}",
            formatear_cop(c_u_fl),
            formatear_cop(c_tot_fl),
        ])

    for cat_nom, lista_items in categorizados.items():
        elements.append(Paragraph(f"📂 <b>Categoría: {cat_nom}</b>", cat_header_style))
        
        tabla_data = [["Insumo", "Stock Actual", "Costo U.", "Costo Total Insumo"]]

        for row in lista_items:
            tabla_data.append(row)

        t = Table(tabla_data, colWidths=[200, 100, 110, 110])
        t.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8BBD0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#880E4F")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
            ])
        )
        elements.append(t)

    p_total = Paragraph(f"<br/><br/><b>COSTO TOTAL VALORIZADO DE INSUMOS EN BODEGA: {formatear_cop(total_general_costo)}</b>", titulo_style)
    elements.append(p_total)

    doc.build(elements)
    buffer.seek(0)
    return buffer


def generar_pdf_agotados_sync():
    agotados = stock_bajo_sync()
    if not agotados:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()

    titulo_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, textColor=colors.HexColor("#D81B60"), spaceAfter=8
    )
    sub_style = ParagraphStyle(
        "SubStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, textColor=colors.gray, spaceAfter=15
    )

    now_co = datetime.now(COLOMBIA_TZ)
    elements = [
        Paragraph("Soulcerón - Reporte de Productos Agotados", titulo_style),
        Paragraph(f"Generado el: {now_co.strftime('%Y-%m-%d %H:%M:%S')}", sub_style),
    ]

    tabla_data = [["Producto", "Stock", "Costo U.", "Precio Venta", "Proveedor"]]

    for nombre, stock, costo, precio, proveedor in agotados:
        tabla_data.append([
            str(nombre),
            str(stock),
            formatear_cop(costo),
            formatear_cop(precio),
            str(proveedor),
        ])

    t = Table(tabla_data, colWidths=[185, 45, 85, 85, 150])
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8BBD0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#880E4F")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F5F5F5")),
        ])
    )

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer


# -------------------------------------------------------------------
# SEGMENTACIÓN DE CLIENTES Y ANÁLISIS DE PRODUCTOS/COMBOS
# -------------------------------------------------------------------
def consultar_segmentacion_sync():
    conn = get_db_connection()
    cur = conn.cursor()

    query_top = """
        WITH ranked_clientes AS (
            SELECT 
                nombre,
                categoria_calculada,
                total_compras,
                (total_contado + total_credito) AS total_monto,
                ROW_NUMBER() OVER (PARTITION BY categoria_calculada ORDER BY (total_contado + total_credito) DESC) as rn
            FROM vista_clientes_segmentados
            WHERE total_compras > 0
        )
        SELECT nombre, categoria_calculada, total_compras, total_monto
        FROM ranked_clientes
        WHERE rn <= 3
        ORDER BY categoria_calculada ASC, total_monto DESC;
    """
    cur.execute(query_top)
    top_clientes = cur.fetchall()

    query_inactivos = """
        SELECT nombre, dias_sin_comprar, ultima_compra
        FROM vista_clientes_segmentados
        WHERE dias_sin_comprar > 30 OR ultima_compra IS NULL
        ORDER BY dias_sin_comprar DESC NULLS LAST;
    """
    cur.execute(query_inactivos)
    inactivos = cur.fetchall()

    cur.close()
    conn.close()

    msg_lineas = ["👥 <b>REPORTE DE SEGMENTACIÓN Y CLIENTES</b> 👥\n"]

    if top_clientes:
        msg_lineas.append("🏆 <b>Top 3 Clientes por Categoría:</b>")
        cats = {}
        for nom, cat, cant, monto in top_clientes:
            cats.setdefault(cat, []).append(
                f"  • <b>{nom}</b> — {cant} compras | Total: <code>{formatear_cop(monto)}</code>"
            )

        for cat_name, lista in cats.items():
            msg_lineas.append(f"\n🏷️ <b>Categoría {cat_name}:</b>")
            msg_lineas.extend(lista)
    else:
        msg_lineas.append("ℹ️ <i>No se registran compras para clasificar el top de clientes.</i>")

    msg_lineas.append("\n" + "─" * 28 + "\n")
    msg_lineas.append("⏳ <b>Clientes Inactivos (> 1 Mes sin comprar):</b>")

    if inactivos:
        for nom, dias, ult_fecha in inactivos:
            if dias is not None:
                dias_int = int(dias)
                msg_lineas.append(f"• <b>{nom}</b> — Hace <code>{dias_int} días</code>")
            else:
                msg_lineas.append(f"• <b>{nom}</b> — <i>Sin compras registradas</i>")
    else:
        msg_lineas.append("✨ <i>¡Excelente! Todos los clientes han comprado este último mes.</i>")

    return "\n".join(msg_lineas)


def consultar_analisis_productos_sync():
    conn = get_db_connection()
    cur = conn.cursor()

    query_mejor_ganancia = """
        SELECT nombre, costo_compra, precio_venta, (precio_venta - costo_compra) as ganancia_u
        FROM productos
        ORDER BY (precio_venta - costo_compra) DESC
        LIMIT 3;
    """
    cur.execute(query_mejor_ganancia)
    mejores = cur.fetchall()

    query_peor_ganancia = """
        SELECT nombre, costo_compra, precio_venta, (precio_venta - costo_compra) as ganancia_u
        FROM productos
        ORDER BY (precio_venta - costo_compra) ASC
        LIMIT 3;
    """
    cur.execute(query_peor_ganancia)
    peores = cur.fetchall()

    query_estrellas = """
        SELECT p.nombre, SUM(dv.cantidad) as total_unidades, COUNT(DISTINCT dv.id_venta) as total_facturas
        FROM detalle_ventas dv
        JOIN productos p ON dv.id_producto = p.id_producto
        GROUP BY p.id_producto, p.nombre
        ORDER BY total_unidades DESC
        LIMIT 3;
    """
    cur.execute(query_estrellas)
    estrellas = cur.fetchall()

    query_combos = """
        SELECT p1.nombre as prod1, p2.nombre as prod2, COUNT(*) as veces_juntos
        FROM detalle_ventas dv1
        JOIN detalle_ventas dv2 ON dv1.id_venta = dv2.id_venta AND dv1.id_producto < dv2.id_producto
        JOIN productos p1 ON dv1.id_producto = p1.id_producto
        JOIN productos p2 ON dv2.id_producto = p2.id_producto
        GROUP BY p1.nombre, p2.nombre
        HAVING COUNT(*) > 5
        ORDER BY veces_juntos DESC;
    """
    cur.execute(query_combos)
    combos = cur.fetchall()

    cur.close()
    conn.close()

    msg_lineas = ["📊 <b>ANÁLISIS DE PRODUCTOS Y COMBOS</b> 📊\n"]

    msg_lineas.append("💎 <b>Top 3 Productos con Mejor Ganancia Unitario:</b>")
    for nom, costo, precio, gan in mejores:
        msg_lineas.append(
            f"• <b>{nom}</b> — Ganancia: <code>+{formatear_cop(gan)}</code>\n   └ Costo: {formatear_cop(costo)} | Venta: {formatear_cop(precio)}"
        )

    msg_lineas.append("\n⚠️ <b>Top 3 Productos con Menor Ganancia Unitario:</b>")
    for nom, costo, precio, gan in peores:
        msg_lineas.append(
            f"• <b>{nom}</b> — Ganancia: <code>+{formatear_cop(gan)}</code>\n   └ Costo: {formatear_cop(costo)} | Venta: {formatear_cop(precio)}"
        )

    msg_lineas.append("\n🌟 <b>Top 3 Productos Estrella (Más Vendidos):</b>")
    if estrellas:
        for nom, uds, facturas in estrellas:
            msg_lineas.append(f"• <b>{nom}</b> — <code>{uds} uds.</code> vendidas (en {facturas} facturas)")
    else:
        msg_lineas.append("ℹ️ <i>Aún no hay suficientes ventas registradas.</i>")

    msg_lineas.append("\n" + "─" * 28 + "\n")
    msg_lineas.append("🔥 <b>Posibles Combos Sugeridos:</b>")

    if combos:
        for p1, p2, veces in combos:
            msg_lineas.append(f"💡 <b>{p1}</b> + <b>{p2}</b>\n   └ Comprados juntos en <code>{veces} facturas</code>")
    else:
        msg_lineas.append("ℹ️ <i>No se encontraron combos (ningún par de productos supera las 5 facturas juntos).</i>")

    return "\n".join(msg_lineas)


# -------------------------------------------------------------------
# HANDLER PRINCIPAL DE BOTONES (CALLBACK QUERY)
# -------------------------------------------------------------------
async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    try:
        if data == "btn_menu_principal":
            await start(update, context)

        elif data == "btn_guia_venta":
            msg = (
                "📝 <b>Para registrar una venta envía un mensaje con este formato:</b>\n\n"
                "<pre>\n"
                "v\n"
                "Cliente Nombre\n"
                "contado\n"
                "Combo Chocolates x5, 1\n"
                "Labial Mágico, 2\n"
                "</pre>"
            )
            await query.message.reply_text(msg, parse_mode="HTML")

        elif data == "btn_menu_compras":
            await mostrar_categorias_compras(update, context)

        elif data.startswith("compra_cat_"):
            await seleccionar_insumo_compra(update, context)

        elif data == "btn_menu_balances":
            msg = "📊 <b>Módulo de Balances Financieros</b>\n\nSelecciona el periodo que deseas consultar:"
            await query.message.reply_text(msg, reply_markup=obtener_teclado_balances(), parse_mode="HTML")

        elif data.startswith("recetas_page_"):
            await mostrar_costos_recetas_paginado(update, context)

        elif data.startswith("rec_add_page_"):
            await opciones_agregar_insumo_receta_paginado(update, context)

        elif data.startswith("ver_receta_"):
            await ver_detalle_receta(update, context)

        elif data.startswith("rec_del_"):
            await opciones_quitar_insumo_receta(update, context)

        elif data.startswith("confirm_del_rec_"):
            await ejecutar_quitar_insumo_receta(update, context)

        elif data.startswith("rec_mod_"):
            await opciones_modificar_cantidad_receta(update, context)

        elif data == "btn_ventas_hoy":
            datos_hoy = ventas_hoy_sync()
            msg = construir_mensaje_resumen_completo("Ventas del Día de Hoy", datos_hoy)
            pdf_buffer = generar_pdf_dia_sync()
            now_co = datetime.now(COLOMBIA_TZ)

            await query.message.reply_text(enviar_mensaje_seguro(msg), parse_mode="HTML")

            if pdf_buffer:
                pdf_buffer.seek(0)
                await query.message.reply_document(
                    document=pdf_buffer,
                    filename=f"Reporte_Diario_{now_co.strftime('%d_%m_%Y')}.pdf",
                    caption="📄 Reporte PDF con el desglose de ventas de hoy.",
                )

            await query.message.reply_text(
                "👇 <i>Selecciona una opción para continuar:</i>",
                reply_markup=obtener_teclado_balances(),
                parse_mode="HTML",
            )

        elif data == "btn_ventas_semana":
            dias, datos_semana = ventas_semana_sync(0)
            if datos_semana["ventas"] == 0:
                msg_final = "ℹ️ <b>No se encontraron ventas registradas en lo que va de esta semana.</b>"
                await query.message.reply_text(enviar_mensaje_seguro(msg_final), parse_mode="HTML")
            else:
                msg_resumen = construir_mensaje_resumen_completo("Balance de la Semana (En curso)", datos_semana)
                pdf_buffer = generar_pdf_semana_sync()
                now_co = datetime.now(COLOMBIA_TZ)

                await query.message.reply_text(enviar_mensaje_seguro(msg_resumen), parse_mode="HTML")

                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Balance_Semanal_{now_co.strftime('%d_%m_%Y')}.pdf",
                        caption="📄 Reporte PDF semanal hasta la fecha.",
                    )

            await query.message.reply_text(
                "👇 <i>Selecciona una opción para continuar:</i>",
                reply_markup=obtener_teclado_balances(),
                parse_mode="HTML",
            )

        elif data == "btn_ventas_mes":
            datos_mes = ventas_mes_sync(0)
            if datos_mes["ventas"] == 0:
                msg_final = "ℹ️ <b>No se encontraron ventas registradas en lo que va de este mes.</b>"
                await query.message.reply_text(enviar_mensaje_seguro(msg_final), parse_mode="HTML")
            else:
                msg_resumen = construir_mensaje_resumen_completo("Balance del Mes (En curso)", datos_mes)
                pdf_buffer = generar_pdf_mes_sync(mes_offset=0)
                now_co = datetime.now(COLOMBIA_TZ)

                await query.message.reply_text(enviar_mensaje_seguro(msg_resumen), parse_mode="HTML")

                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Balance_Mensual_{now_co.strftime('%m_%Y')}.pdf",
                        caption="📄 Reporte PDF mensual hasta la fecha.",
                    )

            await query.message.reply_text(
                "👇 <i>Selecciona una opción para continuar:</i>",
                reply_markup=obtener_teclado_balances(),
                parse_mode="HTML",
            )

        elif data == "btn_valorizacion":
            costo, venta = valorizacion_sync()
            ganancia_est = venta - costo
            
            msg = (
                "🏢 <b>Valorización & Inventario General de Bodega</b>\n\n"
                f"💵 <b>Costo General Invertido:</b> <code>{formatear_cop(costo)}</code>\n"
                f"📈 <b>Precio / Valor Potencial Venta:</b> <code>{formatear_cop(venta)}</code>\n"
                f"💎 <b>Ganancia Potencial Estimada:</b> <code>{formatear_cop(ganancia_est)}</code>\n\n"
                "📄 <i>Adjunto encontrarás el reporte PDF con el inventario completo bien categorizado.</i>"
            )
            
            pdf_buffer = generar_pdf_valorizacion_y_categorizado_sync()
            now_co = datetime.now(COLOMBIA_TZ)

            await query.message.reply_text(enviar_mensaje_seguro(msg), parse_mode="HTML")
            if pdf_buffer:
                pdf_buffer.seek(0)
                await query.message.reply_document(
                    document=pdf_buffer,
                    filename=f"Inventario_Categorizado_{now_co.strftime('%d_%m_%Y')}.pdf",
                )

            await query.message.reply_text(
                "👇 <i>Selecciona una opción del menú para continuar:</i>",
                reply_markup=obtener_teclado_menu(),
                parse_mode="HTML",
            )

        elif data == "btn_stock_bajo":
            filas = stock_bajo_sync()
            if not filas:
                await query.message.reply_text(
                    "✅ <b>No se encontraron productos agotados. ¡Tu stock está al día!</b>",
                    parse_mode="HTML",
                )
            else:
                lineas = [
                    "🚫 <b>Productos Totalmente Agotados (0 uds.)</b>\n",
                    "📄 <i>Adjunto encontrarás el reporte PDF con la lista completa.</i>",
                ]
                for n, s, c, p, prov in filas:
                    lineas.append(f"• <b>{n}</b>")

                pdf_buffer = generar_pdf_agotados_sync()
                now_co = datetime.now(COLOMBIA_TZ)

                await query.message.reply_text(enviar_mensaje_seguro("\n".join(lineas)), parse_mode="HTML")
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    await query.message.reply_document(
                        document=pdf_buffer,
                        filename=f"Productos_Agotados_{now_co.strftime('%d_%m_%Y')}.pdf",
                    )

            await query.message.reply_text(
                "👇 <i>Selecciona una opción del menú para continuar:</i>",
                reply_markup=obtener_teclado_menu(),
                parse_mode="HTML",
            )

        elif data == "btn_segmentacion":
            msg_seg = consultar_segmentacion_sync()
            await query.message.reply_text(enviar_mensaje_seguro(msg_seg), parse_mode="HTML")
            await query.message.reply_text(
                "👇 <i>Selecciona una opción del menú para continuar:</i>",
                reply_markup=obtener_teclado_menu(),
                parse_mode="HTML",
            )

        elif data == "btn_analisis_productos":
            msg_prod = consultar_analisis_productos_sync()
            await query.message.reply_text(enviar_mensaje_seguro(msg_prod), parse_mode="HTML")
            await query.message.reply_text(
                "👇 <i>Selecciona una opción del menú para continuar:</i>",
                reply_markup=obtener_teclado_menu(),
                parse_mode="HTML",
            )

        elif data == "btn_ayuda":
            await ayuda(update, context)

    except Exception as e:
        logging.error(f"Error en callback: {e}")
        await query.message.reply_text(f"🔴 <b>Error al procesar la solicitud:</b> {str(e)}", parse_mode="HTML")


# -------------------------------------------------------------------
# MANIPULADOR DE MENSAJES DE TEXTO
# -------------------------------------------------------------------
async def procesar_mensaje_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if update.update_id in UPDATES_PROCESADOS:
        logging.info(f"Update ID {update.update_id} omitido por desduplicación de webhook.")
        return
    UPDATES_PROCESADOS.add(update.update_id)

    if len(UPDATES_PROCESADOS) > 1000:
        UPDATES_PROCESADOS.clear()

    texto = update.message.text.strip()
    primera_linea = texto.split("\n")[0].strip().lower()

    if primera_linea.startswith("/"):
        primera_linea = primera_linea[1:]

    if primera_linea in ["v", "venta"]:
        await registrar_venta(update, context)

    elif primera_linea in ["c", "compra"]:
        await registrar_compra_maquillaje_rapida(update, context)

    elif (
        primera_linea.startswith("n ")
        or primera_linea.startswith("nombre ")
        or primera_linea.startswith("cliente ")
    ):
        await consultar_cliente(update, context)

    elif primera_linea.startswith("p ") or primera_linea.startswith("producto "):
        await consultar_producto(update, context)

    elif primera_linea in ["m", "menu", "start"]:
        await start(update, context)

    elif primera_linea in ["ayuda", "help"]:
        await ayuda(update, context)


# -------------------------------------------------------------------
# TAREAS AUTOMÁTICAS PROGRAMADAS
# -------------------------------------------------------------------
def mantener_vivo():
    try:
        urllib.request.urlopen("https://soulceron.onrender.com/", timeout=5)
        logging.info("Ping de mantenimiento exitoso a Render.")
    except Exception as e:
        logging.error(f"Error en ping de mantenimiento: {e}")


def tarea_saludo_manana():
    if TELEGRAM_TOKEN:
        try:
            admins = obtener_lista_admins()
            for admin_id in admins:
                if str(admin_id) == ID_ESPOSA:
                    msg = (
                        "☀️ <b>¡Buenos días!</b> ☀️\n\nRecuerda que estoy aquí para"
                        " ayudarte a llevar tu negocio y vamos con toda el día de hoy, <b>Mi"
                        " barrigona hermosa</b> 💖✨"
                    )
                else:
                    msg = (
                        "☀️ <b>¡Buenos días!</b> ☀️\n\nRecuerda que estoy aquí para"
                        " ayudarte a llevar tu negocio y vamos con toda el día de hoy 💪✨"
                    )

                enviar_mensaje_api(admin_id, msg)
        except Exception as e:
            logging.error(f"Error en saludo de la mañana: {e}")


def tarea_cierre_diario():
    if TELEGRAM_TOKEN:
        try:
            datos_hoy = ventas_hoy_sync()
            msg = (
                "🔔 <b>Cierre de Caja Automático (7:00 PM)</b> 🔔\n\n"
                f"{construir_mensaje_resumen_completo('Resumen de Hoy', datos_hoy)}\n\n"
                "📄 <i>Adjunto encontrarás el reporte PDF con el desglose del día.</i>"
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
                        "📄 Reporte PDF Diario",
                    )
        except Exception as e:
            logging.error(f"Error en cierre diario automático: {e}")


def tarea_cierre_semanal():
    if TELEGRAM_TOKEN:
        try:
            _, datos_semana = ventas_semana_sync(0)
            msg_resumen = (
                "📊 <b>BALANCE AUTOMÁTICO SEMANAL (DOMINGO 8:00 PM)</b> 📊\n\n"
                f"{construir_mensaje_resumen_completo('Consolidado Semana', datos_semana)}"
            )
            msg_comparativa = construir_comparativa_semanal()
            pdf_buffer = generar_pdf_semana_sync()
            now_co = datetime.now(COLOMBIA_TZ)
            admins = obtener_lista_admins()

            for admin_id in admins:
                enviar_mensaje_api(admin_id, msg_resumen)
                enviar_mensaje_api(admin_id, msg_comparativa)
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    enviar_documento_api(
                        admin_id,
                        pdf_buffer,
                        f"Reporte_Semanal_{now_co.strftime('%d_%m_%Y')}.pdf",
                        "📄 Reporte PDF Semanal",
                    )
        except Exception as e:
            logging.error(f"Error en cierre semanal automático: {e}")


def tarea_cierre_mensual_automatico():
    if TELEGRAM_TOKEN:
        try:
            datos_mes_cerrado = ventas_mes_sync(1)
            msg_resumen = (
                "📈 <b>REPORTE AUTOMÁTICO MENSUAL (1° DEL MES - 8:00 AM)</b> 📈\n\n"
                f"{construir_mensaje_resumen_completo('Consolidado Mes Anterior', datos_mes_cerrado)}"
            )
            msg_comparativa = construir_comparativa_mensual()
            pdf_buffer = generar_pdf_mes_sync(mes_offset=1)
            now_co = datetime.now(COLOMBIA_TZ)
            admins = obtener_lista_admins()

            for admin_id in admins:
                enviar_mensaje_api(admin_id, msg_resumen)
                enviar_mensaje_api(admin_id, msg_comparativa)
                if pdf_buffer:
                    pdf_buffer.seek(0)
                    enviar_documento_api(
                        admin_id,
                        pdf_buffer,
                        f"Reporte_Mensual_Anterior_{now_co.strftime('%m_%Y')}.pdf",
                        "📄 Reporte PDF Mensual",
                    )
        except Exception as e:
            logging.error(f"Error en reporte mensual automático: {e}")


# Inicialización del Scheduler
scheduler = BackgroundScheduler(timezone=COLOMBIA_TZ)
scheduler.add_job(mantener_vivo, "interval", minutes=10)
scheduler.add_job(
    tarea_saludo_manana, "cron", hour=7, minute=0, timezone=COLOMBIA_TZ
)
scheduler.add_job(
    tarea_cierre_diario, "cron", hour=19, minute=0, timezone=COLOMBIA_TZ
)
scheduler.add_job(
    tarea_cierre_semanal,
    "cron",
    day_of_week="sun",
    hour=20,
    minute=0,
    timezone=COLOMBIA_TZ,
)
scheduler.add_job(
    tarea_cierre_mensual_automatico,
    "cron",
    day=1,
    hour=8,
    minute=0,
    timezone=COLOMBIA_TZ,
)
scheduler.start()

# -------------------------------------------------------------------
# INICIALIZACIÓN DE HANDLERS Y CONVERSACIONES
# -------------------------------------------------------------------
ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

conv_compras = ConversationHandler(
    entry_points=[CallbackQueryHandler(pedir_datos_compra, pattern="^compra_ins_|^compra_nuevo_")],
    states={ESTADO_CANTIDAD_COSTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_compra_menu)]},
    fallbacks=[CommandHandler("cancelar", cancelar_conversion)],
)

conv_mod_receta = ConversationHandler(
    entry_points=[CallbackQueryHandler(pedir_nueva_cantidad_receta, pattern="^pedir_mod_cant_")],
    states={ESTADO_EDITAR_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_nueva_cantidad_receta)]},
    fallbacks=[CommandHandler("cancelar", cancelar_conversion)],
)

conv_add_receta = ConversationHandler(
    entry_points=[CallbackQueryHandler(pedir_cantidad_agregar_receta, pattern="^sel_add_ins_")],
    states={ESTADO_AGREGAR_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_agregar_insumo_receta)]},
    fallbacks=[CommandHandler("cancelar", cancelar_conversion)],
)

conv_crear_combo = ConversationHandler(
    entry_points=[CallbackQueryHandler(iniciar_crear_combo, pattern="^btn_crear_combo$")],
    states={ESTADO_CREAR_COMBO_DATOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_crear_combo)]},
    fallbacks=[CommandHandler("cancelar", cancelar_conversion)],
)

ptb_app.add_handler(conv_compras)
ptb_app.add_handler(conv_mod_receta)
ptb_app.add_handler(conv_add_receta)
ptb_app.add_handler(conv_crear_combo)
ptb_app.add_handler(CallbackQueryHandler(manejar_callback))
ptb_app.add_handler(CommandHandler("test_notificaciones", probar_notificaciones))
ptb_app.add_handler(MessageHandler(filters.ALL, procesar_mensaje_texto))


@web_app.route("/", methods=["GET"])
def home():
    return "Bot de Soulcerón Activo con Menú Interactivo."


@web_app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "POST":
        json_data = request.get_json(force=True)

        async def process():
            async with ptb_app:
                update = Update.de_json(json_data, ptb_app.bot)
                await ptb_app.process_update(update)

        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(process())
        else:
            asyncio.run(process())

        return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)
