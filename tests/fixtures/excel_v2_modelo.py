"""El caso canonico del Excel aprobado, congelado como fixture.

Sale de `Cotizador_Greda_V2_modelo.xlsx`, hoja a hoja, leyendo los valores que
el propio Excel tiene cacheados —no recalculados por nadie—. Cada numero de
aqui es lo que la hoja muestra.

## Por que se copia en vez de leer el fichero

Porque el Excel es fuente de DISENO y de PRUEBA, no una dependencia de
ejecucion. Un test que abriera el `.xlsx` fallaria en la CI, obligaria a
versionar un binario de 83 KB y ataria el motor economico a que alguien no
guarde el fichero con otra version de Excel. Lo que hay que fijar es el
RESULTADO ACORDADO, y eso es texto.

## Como se leyeron

    import openpyxl
    wb = openpyxl.load_workbook(ruta, data_only=True)   # valores cacheados
    wb["Cotizador V2"]["B26"].value                     # 2558.4266666666667

Los formularios se leyeron con `data_only=False` sobre el mismo fichero.

## La estructura economica que el Excel define

    costo directo del producto        = materiales + mano de obra + ilustracion
    quema asignada                    = % VOLUMEN        x quema comercial
    espacio asignado                  = % HORAS de MO    x costo del espacio
    administracion asignada           = % COSTO DIRECTO  x administracion
    costo de produccion asignado      = directo + quema + espacio + admin
    costo real asignado               = directo + gas    + espacio + admin
    precio de la linea                = costo de produccion asignado x factor
    precio unitario                   = precio / cantidad / tipo de cambio
    precio unitario comercial         = ROUNDUP(unitario / 0,50) x 0,50
    subtotal de la linea              = unitario comercial x cantidad
    IGV                               = subtotal x 18 %

## La unica divergencia, y es deliberada

El Excel lleva la ilustracion por PRODUCTO —sus S/44 caen enteros sobre «Plato
palta»—. El sistema la lleva por COTIZACION, porque asi lo fijo la regla
aprobada de 010D: «la ilustracion es una sola por cotizacion y no una tecnica
mas». Esa regla manda sobre el Excel.

La consecuencia es exacta y esta acotada: **todos los totales coinciden** y lo
unico que cambia es a que linea se le carga cada parte de esos S/44. Las
pruebas lo comprueban en los dos sentidos —los globales contra el Excel, y el
reparto por linea alimentandolo con el costo directo del Excel—.
"""

from __future__ import annotations

from decimal import Decimal

#: Ruta del fichero del que salen estos numeros. Documental: ninguna prueba la
#: abre.
EXCEL_PATH = r"C:\Users\anthg\Downloads\excel7\Cotizador_Greda_V2_modelo.xlsx"

#: Las ocho hojas del libro, en orden.
EXCEL_SHEETS = (
    "Configuración",
    "Cotizador V2",
    "Mano de obra",
    "Ilustración",
    "Quema V2",
    "Productos",
    "PDF cliente",
    "Reglas",
)

# ---------------------------------------------------------------------------
# Configuracion vigente en el Excel
# ---------------------------------------------------------------------------
TAX_PERCENT = Decimal(18)
ROUNDING_STEP = Decimal("0.5")
COMMERCIAL_FACTOR = Decimal(3)
FACTOR_MIN = Decimal(2)
FACTOR_MAX = Decimal(3)
EXCHANGE_RATE = Decimal("3.5")
SPACE_PER_DAY = Decimal(140)
ADMIN_PER_QUOTE = Decimal(200)
EFFECTIVE_WORK_DAYS = 4
SMALL_KILN_CAPACITY = Decimal(17000)

# ---------------------------------------------------------------------------
# Las tres lineas del caso canonico
# ---------------------------------------------------------------------------
#: Por producto: cantidad, volumen total, materiales, mano de obra, horas de
#: mano de obra e ilustracion tal y como el Excel los reparte.
LINES = (
    {
        "name": "Plato palta",
        "quantity": 20,
        "total_volume_cm3": Decimal(12960),
        "materials_cost": Decimal("173.7"),
        "labor_cost": Decimal("190.66666666666666"),
        "labor_hours": Decimal("13.866666666666667"),
        "illustration_cost": Decimal(44),
        # Lo que el Excel calcula para esta linea.
        "volume_share": Decimal("0.4982698961937716"),
        "hours_share": Decimal("0.4693140794223827"),
        "firing_allocated": Decimal("448.44290657439444"),
        "space_allocated": Decimal("262.8158844765343"),
        "production_allocated": Decimal("1210.5325248868692"),
        "real_allocated": Decimal("866.7262965131666"),
        "unit_price_raw": Decimal("181.57987873303037"),
        "unit_price": Decimal(182),
        "line_subtotal": Decimal(3640),
        "line_tax": Decimal("655.2"),
        "line_total": Decimal("4295.2"),
    },
    {
        "name": "Tasa Buho",
        "quantity": 50,
        "total_volume_cm3": Decimal(2250),
        "materials_cost": Decimal("19.5"),
        "labor_cost": Decimal(220),
        "labor_hours": Decimal(8),
        "illustration_cost": Decimal(0),
        "volume_share": Decimal("0.08650519031141868"),
        "hours_share": Decimal("0.27075812274368233"),
        "firing_allocated": Decimal("77.8546712802768"),
        "space_allocated": Decimal("151.62454873646212"),
        "production_allocated": Decimal("522.2946455012898"),
        "real_allocated": Decimal("462.60606418641083"),
        "unit_price_raw": Decimal("31.337678730077386"),
        "unit_price": Decimal("31.5"),
        "line_subtotal": Decimal(1575),
        "line_tax": Decimal("283.5"),
        "line_total": Decimal("1858.5"),
    },
    {
        "name": "PLATOS HONDOS CHICOS",
        "quantity": 12,
        "total_volume_cm3": Decimal(10800),
        "materials_cost": Decimal("92.16"),
        "labor_cost": Decimal("158.39999999999998"),
        "labor_hours": Decimal("7.68"),
        "illustration_cost": Decimal(0),
        "volume_share": Decimal("0.41522491349480967"),
        "hours_share": Decimal("0.259927797833935"),
        "firing_allocated": Decimal("373.7024221453287"),
        "space_allocated": Decimal("145.5595667870036"),
        "production_allocated": Decimal("825.5994962785078"),
        "real_allocated": Decimal("539.0943059670891"),
        "unit_price_raw": Decimal("206.39987406962692"),
        "unit_price": Decimal("206.5"),
        "line_subtotal": Decimal(2478),
        "line_tax": Decimal("446.04"),
        "line_total": Decimal("2924.04"),
    },
)

# ---------------------------------------------------------------------------
# Los totales de la cabecera, hoja «Cotizador V2»
# ---------------------------------------------------------------------------
TOTALS = {
    # B13:B18
    "materials": Decimal("285.36"),
    "labor": Decimal("569.0666666666666"),
    "illustration": Decimal(44),
    "firing_commercial": Decimal(900),
    "firing_gas": Decimal(210),
    "firing_difference": Decimal(690),
    # B22:B25
    "space": Decimal(560),
    "administration": Decimal(200),
    "extras": Decimal(0),
    # B26:B31
    "production_cost": Decimal("2558.4266666666667"),
    "real_cost": Decimal("1868.4266666666667"),
    "price_min_x2": Decimal("5116.8533333333335"),
    "price_target_x3": Decimal("7675.28"),
    "estimated_profit": Decimal("5824.573333333334"),
    "effective_margin": Decimal("0.7571263919580572"),
    # H13:H17
    "subtotal": Decimal(7693),
    "tax": Decimal("1384.74"),
    "total": Decimal("9077.74"),
    "rounding_adjustment": Decimal("17.72"),
}

#: La ocupacion del caso: 26.010 cm3 en un horno de 17.000 son dos hornadas, y
#: de ahi salen los S/900 de tarifa y los S/210 de gas de 010E.
TOTAL_VOLUME_CM3 = Decimal(26010)
TOTAL_LABOR_HOURS = Decimal("29.546666666666667")

#: Cuanto puede alejarse un resultado del valor que el Excel tiene cacheado.
#: El Excel calcula en coma flotante de doble precision y el sistema en
#: `Decimal`, asi que las ultimas cifras no tienen por que coincidir: lo que se
#: compara es el mismo numero, no la misma representacion.
EXCEL_TOLERANCE = Decimal("0.0000001")
