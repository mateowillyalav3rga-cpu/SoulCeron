import os
import re
import logging
import asyncio
import psycopg2
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

web_app = Flask(__name__)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# -------------------------------------------------------------------
# COMANDO /start Y /ayuda
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ayuda(update, context)

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🤖 **Panel de Control - Bot Soulcerón**\n\n"
        "Aquí tienes la lista de comandos disponibles y su función:\n\n"
        "📝 **Operaciones y Registro:**\n"
        "• `/venta` : Registra una venta en el sistema y descuenta inventario.\n"
        "  *Ejemplo:*\n"
        "  `/venta` \n"
        "  `Cliente: María Paula` \n"
        "  `Pago: contado` \n"
        "  `- Pomo, 1` \n"
        "  `- Donas x2, 1` \n\n"
        "📊 **Reportes Financieros:**\n"
        "• `/ventas_hoy` : Muestra las ventas y recaudación total del día de hoy.\n"
        "• `/ventas_semana` : Muestra el total de ventas y ganancias de la semana.\n"
        "• `/valorizacion` : Muestra el valor total en dinero del inventario en bodega.\n\n"
        "📦 **Inventario y Mantenimiento:**\n"
        "• `/stock_bajo` : Alerta sobre productos con inventario crítico o agotándose.\n\n"
        "💡 **Analítica de Negocio:**\n"
        "• `/combos` : Muestra cuáles productos se suelen comprar juntos habitualmente.\n"
        "• `/top_productos` : Muestra el 20% de productos que generan el 80% de ingresos.\n"
        "• `/clientes` : Segmentación y frecuencia de compra de clientes."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

# -------------------------------------------------------------------
# REGISTRO DE VENTAS (/venta)
# -------------------------------------------------------------------
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
        SELECT id_cliente FROM cliente_existente 
        UNION ALL 
        SELECT id_cliente FROM cliente_creado
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

        total_formateado = f"${total_venta:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")

        respuesta = (
            f"🟢 **¡Venta #{id_venta_registrada} Registrada Exitosamente!**\n\n"
            f"👤 **Cliente:** {cliente_nombre}\n"
            f"💳 **Pago:** {tipo_pago.capitalize()}\n"
            f"📦 **Ítems procesados:** {len(productos_matches)}\n"
            f"💰 **Total Venta:** {total_formateado}"
        )
        await update.message.reply_text(respuesta, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al registrar venta:** {str(e)}")

# -------------------------------------------------------------------
# CONSULTAS Y REPORTES SQL
# -------------------------------------------------------------------
async def ventas_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = """
    SELECT COUNT(v.id_venta) AS total_ventas, COALESCE(SUM(dv.cantidad * dv.precio_unitario), 0) AS total_recaudado
    FROM ventas v
    JOIN detalle_ventas dv ON v.id_venta = dv.id_venta
    WHERE DATE(v.fecha_venta) = CURRENT_DATE;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        res = cur.fetchone()
        cur.close()
        conn.close()

        num_ventas = res[0]
        total = f"${res[1]:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")

        msg = (
            f"📊 **Reporte de Ventas del Día**\n\n"
            f"🔢 **Total Transacciones:** {num_ventas}\n"
            f"💰 **Recaudación Total:** {total}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error en reporte:** {str(e)}")

async def stock_bajo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = """
    SELECT nombre, stock_actual 
    FROM productos 
    WHERE stock_actual <= 5 
    ORDER BY stock_actual ASC;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        filas = cur.fetchall()
        cur.close()
        conn.close()

        if not filas:
            await update.message.reply_text("✅ **Todo el inventario está en niveles óptimos.**", parse_mode="Markdown")
            return

        lineas = ["⚠️ **Alerta de Stock Bajo (5 o menos unidades):**\n"]
        for nombre, stock in filas:
            lineas.append(f"• **{nombre}**: {stock} unidades restantes")
        
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al consultar stock:** {str(e)}")

async def valorizacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = """
    SELECT SUM(stock_actual * costo_compra) AS costo_total, SUM(stock_actual * precio_venta) AS valor_venta_total
    FROM productos;
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        res = cur.fetchone()
        cur.close()
        conn.close()

        costo = f"${res[0] or 0:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
        venta = f"${res[1] or 0:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")

        msg = (
            f"🏢 **Valorización Total del Inventario en Bodega**\n\n"
            f"💵 **Costo Total Invertido:** {costo}\n"
            f"📈 **Valor de Venta estimado:** {venta}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"🔴 **Error al calcular valorización:** {str(e)}")

# -------------------------------------------------------------------
# RUTAS DE FLASK Y WEBHOOK
# -------------------------------------------------------------------
@web_app.route('/', methods=['GET'])
def home():
    return "Bot de Soulcerón Webhook Activo."

@web_app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        json_data = request.get_json(force=True)
        
        async def process():
            ptb_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
            
            # Registro de manejadores de comandos
            ptb_app.add_handler(CommandHandler("start", start))
            ptb_app.add_handler(CommandHandler("ayuda", ayuda))
            ptb_app.add_handler(CommandHandler("venta", registrar_venta))
            ptb_app.add_handler(CommandHandler("ventas_hoy", ventas_hoy))
            ptb_app.add_handler(CommandHandler("stock_bajo", stock_bajo))
            ptb_app.add_handler(CommandHandler("valorizacion", valorizacion))
            
            ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r"(?i)^/venta"), registrar_venta))
            
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
