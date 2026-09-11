"""Valores de arranque de la configuracion comercial del Cotizador V2.

Fase 010B. Todo lo que aqui aparece es un **valor por defecto**, no una
constante del motor: la casa los edita desde Configuracion y cada cotizacion se
lleva su copia. Ninguna formula de 010C en adelante puede leer este modulo para
calcular; solo la migracion y el alta de la configuracion los usan como punto
de partida.

Los numeros no son inventados: salen del Excel «Cotizador Greda V2» aprobado y
de las reglas cerradas del proyecto. Por eso se siembran, a diferencia de las
tarifas de prototipo de 009K.1.1, que nacieron en cero porque su Excel las
marcaba explicitamente como EJEMPLO.

La excepcion son las tarifas de horno: ver `V2_REFERENCE_KILN_RATES`.
"""

from __future__ import annotations

from decimal import Decimal

# ---------------------------------------------------------------------------
# Jornada y costos generales
# ---------------------------------------------------------------------------
#: Horas de una jornada estandar. De aqui sale la tarifa por hora de cualquier
#: trabajo: jornal / horas. Una jornada de 0 horas haria una division por cero,
#: asi que el CHECK exige > 0.
DEFAULT_WORKDAY_HOURS = Decimal("8")

#: Espacio y servicios del taller, por DIA EFECTIVO de uso. No por dia de
#: vigencia de la oferta ni por dia de calendario: si la produccion ocupa el
#: taller cuatro dias, se cobran cuatro.
DEFAULT_SPACE_SERVICE_COST_PER_DAY = Decimal("140")

#: Administracion, UNA VEZ por cotizacion. No por producto y no por pieza:
#: cotizar mil piezas cuesta administrativamente lo mismo que cotizar diez.
DEFAULT_ADMINISTRATIVE_COST_PER_QUOTE = Decimal("200")

# ---------------------------------------------------------------------------
# Factor comercial
# ---------------------------------------------------------------------------
#: Factor, no porcentaje: 3 significa x3. Se guarda como factor porque es como
#: lo expresa el negocio y como lo define el Excel; convertirlo a 300 % por
#: dentro obligaria a traducir en cada frontera y a acertar siempre.
DEFAULT_COMMERCIAL_FACTOR = Decimal("3")
#: Suelo duro. Vender por debajo de x2 exigira una autorizacion que todavia no
#: existe, asi que hoy el minimo es tambien el limite inferior admitido.
DEFAULT_COMMERCIAL_FACTOR_MIN = Decimal("2")
DEFAULT_COMMERCIAL_FACTOR_MAX = Decimal("3")

# ---------------------------------------------------------------------------
# Vigencia
# ---------------------------------------------------------------------------
#: Dias que se respeta la oferta economica. NO es el plazo de produccion ni el
#: tiempo que tarda el pedido: es cuanto dura el precio.
DEFAULT_QUOTATION_VALIDITY_DAYS = 20

# ---------------------------------------------------------------------------
# Moneda
# ---------------------------------------------------------------------------
#: Moneda base del taller. El IGV, el simbolo y el catalogo de monedas se leen
#: de `commercial_settings`, que es la fuente canonica: V2 NO tiene un IGV
#: propio ni un catalogo paralelo.
BASE_CURRENCY = "PEN"
#: Tipo de cambio de referencia PEN/USD. Es un valor MANUAL, como en Legacy: no
#: hay proveedor automatico de FX en el proyecto y esta fase no inventa uno.
DEFAULT_EXCHANGE_RATE = Decimal("3.5")

# ---------------------------------------------------------------------------
# Ilustracion
# ---------------------------------------------------------------------------
#: Jornal de ilustracion. Independiente de la mano de obra tecnica: ilustrar no
#: es tornear.
DEFAULT_ILLUSTRATION_DAILY_RATE = Decimal("110")
#: Piezas ilustradas por jornada. La tarifa por hora y el rendimiento por hora
#: se DERIVAN de esto y de la jornada global; no se guardan aparte para que no
#: puedan contradecirse.
DEFAULT_ILLUSTRATION_PIECES_PER_WORKDAY = Decimal("50")

# ---------------------------------------------------------------------------
# Tarifas de horno de referencia
# ---------------------------------------------------------------------------
#: Los numeros aprobados para un horno chico y uno grande.
#:
#: NO se siembran automaticamente. El taller da de alta sus hornos con nombre y
#: capacidad propios, y el sistema no sabe —ni debe adivinar por un umbral de
#: capacidad que nadie definio— cual de ellos es «el chico». Sembrarlos por
#: suposicion pondria una tarifa de 200 soles en el horno equivocado, y eso se
#: descubre cuando ya se envio la cotizacion.
#:
#: Se exponen como referencia en la API de configuracion para que la pantalla
#: pueda ofrecerlos de un clic, y sea una persona quien diga a que horno
#: corresponde cada columna.
V2_REFERENCE_KILN_RATES: dict[str, dict[str, Decimal]] = {
    "SMALL": {
        "gas_cost_low": Decimal("35"),
        "gas_cost_high": Decimal("70"),
        "external_rate_low": Decimal("200"),
        "external_rate_high": Decimal("250"),
        "student_rate_low": Decimal("90"),
        "student_rate_high": Decimal("180"),
    },
    "LARGE": {
        "gas_cost_low": Decimal("55"),
        "gas_cost_high": Decimal("110"),
        "external_rate_low": Decimal("700"),
        "external_rate_high": Decimal("1200"),
        "student_rate_low": Decimal("1000"),
        "student_rate_high": Decimal("2000"),
    },
}
