# Sistema documental de Greda

> Desde la Fase 009I.1, **todo PDF oficial se compone con este sistema.**

Greda emite hoy tres documentos y va a emitir más. Antes de 009I.1 cada uno
llevaba su propia hoja de estilos copiada dentro de la plantilla, de modo que
cambiar la tipografía de la empresa significaba editar tantos ficheros como
documentos hubiera —y descubrir semanas después que uno se quedó atrás—.

La regla del sistema es una sola frase:

> **El sistema decide cómo se ve un documento. Cada documento decide qué dice.**

Las dos mitades importan. Si el sistema empieza a saber qué es un IGV, cambiar
el IGV moverá la hoja de taller. Si un documento empieza a definir su propia
tipografía, dejará de ser de la casa el día que la casa cambie.

---

## Qué hay y dónde

```
app/templates/
├── base_document.html              ← el esqueleto. Todo documento lo extiende.
├── styles/
│   └── document_system.css         ← LA identidad visual. Tokens y componentes.
├── components/
│   ├── header.html                 → document_header(company, title, code, badge, meta, aside)
│   ├── footer.html                 → document_footer(text)
│   ├── info_grid.html              → info_grid(rows, columns) / info_section(title, rows)
│   ├── status_badge.html           → status_badge(label, tone)
│   ├── qr_block.html               → qr_block(data_uri, caption, alt)
│   ├── note_block.html             → note_block(text, title)
│   └── signature_block.html        → signature_block(labels, hint)
├── quotations/
│   └── quotation.html              ← contenido comercial
└── production/
    ├── production_order.html       ← contenido de taller
    └── production_public.html      ← contenido público sanitizado
```

Y del lado de Python:

```
app/documents/
├── common.py       ← CompanyDocInfo, DocFact, formatos de fecha, nombre de fichero
├── quotation.py    ← modelo del documento comercial
└── production.py   ← modelo de la hoja de taller Y frontera de lo público
```

---

## Qué es GLOBAL y qué es del documento

| Global (`document_system.css`, `base_document.html`, `components/`) | Del documento (su plantilla) |
| --- | --- |
| Hoja A4, márgenes, saltos de página | Sus columnas y sus anchos |
| Tipografía, tamaños, paleta, espaciados | Sus datos |
| Cabecera de empresa y caja de identidad | Qué pares etiqueta/valor van en la cabecera |
| Tabla base, cabecera de tabla, filas | Qué campos tiene la tabla |
| Distintivo de estado (cinco **tonos**) | Qué **estado** se dibuja con qué tono |
| Nota, QR, firmas, pie, paginación | Qué dice la nota, a dónde apunta el QR, qué firmas hay |

La prueba práctica: **si dejara de tener sentido cuando desaparezca un tipo de
documento, no es global.** El tono `done` es global —significa «terminado y
salió bien»— y el mapa `COMPLETED → done` es de producción, porque el sistema
no sabe que existe `COMPLETED`.

---

## Cómo se añade un documento nuevo

1. **Un modelo tipado propio** en `app/documents/`. Con los campos que ese
   documento necesita y ninguno más. No hay un `GenericDocumentModel` con
   ochenta opcionales, y no debe haberlo: es exactamente lo que 009I.1 separó.
2. **Una plantilla** que extienda `base_document.html` —nunca otro documento— y
   rellene `html_title`, `footer_left`, `content` y, si hace falta,
   `document_styles` y `watermark`.
3. **Sus clases propias con prefijo**: `.quotation-*`, `.production-order-*`,
   `.tracking-*`. Si una clase nueva parece útil para todos, ese es el momento
   de subirla al sistema, no de copiarla.
4. **Decidir qué es público**. Si el documento se va a poder obtener sin sesión,
   la sanitización va en el modelo, no en la plantilla (ver más abajo).
5. **Una prueba que renderice el PDF de verdad y extraiga su texto.** WeasyPrint
   es quien decide qué acaba impreso; un dato tapado con CSS sigue estando en el
   papel que alguien deja sobre una mesa.

---

## Reglas de WeasyPrint (v69)

Esto se renderiza con WeasyPrint, no con Chrome. Lo que funciona en el
navegador no basta como evidencia.

- Maquetar en columnas con `display: table` / `table-cell`. `flex` va a medias
  y `grid` no llega.
- Nada de `position: sticky`, `gap` en flex, `clamp()` ni consultas de
  contenedor.
- Las variables CSS **sí** funcionan (desde la v53). Son la forma de tener
  tokens.
- **Ninguna URL externa.** WeasyPrint no descarga nada: imágenes y fuentes van
  embebidas como `data:`.
- Las cajas de pie de página (`@bottom-left`, `@bottom-right`) aceptan
  `content`, no marcado. Por eso el pie de página es texto y el pie del
  contenido es un componente aparte.
- `page-break-inside: avoid` en cualquier bloque que no deba partirse.

---

## Vocabulario en el CSS compartido

`document_system.css` se incrusta **entero, comentarios incluidos**, en el HTML
de todos los documentos, incluido el que recibe el cliente.

No se escriben ahí palabras del dominio de ningún documento concreto —ni
siquiera en un comentario—. Ya ocurrió: un comentario que explicaba las cajas de
margen de CSS hizo fallar la prueba de privacidad comercial, porque la palabra
llegó al PDF. Lo vigila `tests/unit/test_document_system.py`.

---

## Documentos públicos

Compartir la maquetación **no** puede acabar compartiendo los datos.

La regla es que **la sanitización ocurre en el modelo, antes de la plantilla**.
`PublicTrackingData` (en `app/documents/production.py`) es un `dataclass` con
`slots`: no tiene dónde guardar un almacén, unos gramos o un identificador
aunque alguien se los pase. La plantilla pública no puede enseñar lo que el
modelo no puede contener, así que no necesita acordarse de ocultar nada.

Lo contrario —un modelo único con campos opcionales y un `if es_publico` en la
plantilla— funciona igual de bien hasta el día que alguien añade una fila sin
mirar el `if`.

---

## Cambiar la identidad visual

Para cambiar **todos** los documentos a la vez: `document_system.css`
(o `base_document.html`, o un componente).

Pero un cambio global puede romper un documento concreto: estrechar la columna
de la cabecera puede partir una tabla que cabía justa. Después de tocar el
sistema hay que volver a mirar los tres:

```bash
pytest tests/unit/test_document_system.py tests/unit/test_quotation_pdf_text.py tests/unit/test_quotation_pdf_privacy.py
```

y, con base de datos:

```bash
pytest tests/db/test_production_document.py tests/db/test_tracking_public.py tests/db/test_quotation_pdf_api.py
```

No hay atajo: **compartir estilos reduce el trabajo de cambiar, no el de
comprobar.**
