import os
import re
import logging
import threading
import psycopg2
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

web_app = Flask(__name__)

@web_app.route('/', methods=['GET'])
def home():
    return "Bot de Soulcerón Activo."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "👋 ¡Hola! Soy el bot de registro de ventas de **Soulcerón**.\n\n"
        "Para registrar una venta, envíame un mensaje con esta estructura:\n\n"
        "```\n"
        "/venta\n"
        "Cliente: Nombre Apellido\n"
        "Pago: contado\n"
        "- Nombre Producto, 1\n"
        "- Donas x2, 1\n"
        "```"
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def registrar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text

    cliente_match = re.search(r"Cliente:\s*(.+)", texto, re.IGNORECASE)
    pago_match = re.search(r"Pago:\s*(.+)", texto, re.IGNORECASE)
    productos_matches = re.findall(r"-\s*(.+?),\s*(\d+)", texto)

    if not cliente_match or not pago_match or not productos_matches:
        await update.message.reply_text(
            "❌ **Formato incorrecto.** Usa:\n/venta\nCliente: Nombre\nPago: contado\n- Producto, Cantidad"
        )
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

        if "dona" in prod_clean.lower():
            if "x2" in prod_clean.lower() or "2" in prod_clean:
                sql_block = f"""
                SELECT nv.id_venta, p.id_producto, {cant}, 6000.00
                FROM nueva_venta nv, (
                    SELECT id_producto FROM productos WHERE LOWER(nombre) LIKE LOWER('%dona%') LIMIT 1
                ) p
                """
            else:
                sql_block = f"""
                SELECT nv.id_venta, p.id_producto, {cant}, 3500.00
                FROM nueva_venta nv, (
                    SELECT id_producto FROM productos WHERE LOWER(nombre) LIKE LOWER('%dona%') LIMIT 1
                ) p
                """
        else:
            sql_block = f"""
            SELECT nv.id_venta, p.id_producto, {cant}, p.precio_venta
            FROM nueva_venta nv, (
                SELECT id_producto, precio_venta FROM productos WHERE LOWER(nombre) LIKE LOWER('%{prod_clean}%') LIMIT 1
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
        SELECT id_cliente FROM cliente_existente 
        UNION ALL 
        SELECT id_cliente FROM cliente_creado
    ),
    nueva_venta AS (
        INSERT INTO ventas (id_cliente, tipo_pago)
        SELECT id_cliente, '{tipo_pago}' FROM cliente_final
        RETURNING id_venta
    )
    INSERT INTO detalle_ventas (id_venta, id_producto, cantidad, precio_unitario)
    {query_productos_sql}
    RETURNING id_venta;
    """

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query_completa)
        res = cur.fetchone()
        conn.commit()
        id_venta_registrada = res[0]
        cur.close()
        conn.close()

        respuesta = (
            f"🟢 **¡Venta #{id_venta_registrada} Registrada Exitosamente!**\n\n"
            f"👤 **Cliente:** {cliente_nombre}\n"
            f"💳 **Pago:** {tipo_pago.capitalize()}\n"
            f"📦 **Ítems procesados:** {len(productos_matches)}"
        )
        await update.message.reply_text(respuesta, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar venta:** {str(e)}")

if __name__ == '__main__':
    # Arrancar servidor HTTP para la capa gratuita de Render
    threading.Thread(target=run_flask, daemon=True).start()

    # Arrancar el bot
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("venta", registrar_venta))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/venta"), registrar_venta))

    app.run_polling(drop_pending_updates=True)
