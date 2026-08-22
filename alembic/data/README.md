# Fuentes de los catalogos

- `ubigeo_inei_2022.csv`: 1 891 distritos del conjunto oficial "Ubigeos"
  publicado por INEI en la Plataforma Nacional de Datos Abiertos. Recurso
  original: `UBIGEO 2022_1891 distritos.xlsx`, licencia ODbL.
  https://www.datosabiertos.gob.pe/dataset/ubigeos-c%C3%B3digos-de-ubicaci%C3%B3n-geogr%C3%A1fica-instituto-nacional-de-estad%C3%ADstica-e-inform%C3%A1tica-inei
- `iso_4217_2026.csv`: codigos vigentes de SIX ISO 4217 List One, publicados
  el 2026-01-01. Se excluyen `XTS` (pruebas) y `XXX` (sin moneda). Los nombres
  y simbolos de presentacion provienen de Unicode CLDR para `es-PE`; cuando
  CLDR no define un simbolo local, se usa el propio codigo ISO para evitar
  ambiguedad.
  https://www.six-group.com/en/products-services/financial-information/market-reference-data/data-standards.html
  https://cldr.unicode.org/translation/currency-names-and-symbols

Estos CSV son entradas deterministas de la migracion `0003`: ejecutar una
migracion no realiza descargas ni depende de servicios externos.
