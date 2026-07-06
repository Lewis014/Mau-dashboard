"""Catalogo estructurado de servicios MAU para la generacion del brief (plantilla)."""

# Modulos indexados por la clave que emite la extraccion (modulos_interes).
MODULOS = {
    "comunica": {
        "nombre": "MAU Comunica",
        "desc": "Centraliza las notificaciones SUNAT de multiples RUCs en un solo dashboard, con alertas automaticas.",
    },
    "valida": {
        "nombre": "MAU Valida",
        "desc": "Valida masivamente comprobantes electronicos contra SUNAT, reduciendo el riesgo de perder credito fiscal.",
    },
    "procesa": {
        "nombre": "MAU Procesa",
        "desc": "Automatiza el registro contable de comprobantes, con integracion a CONCAR, EXIGE, Contasis, Starsoft y Odoo.",
    },
}

LINK_TRIAL = "https://bot.contatech.lat/"
LINK_DEMO = "https://calendly.com/contatech/45min"
