# Runbook: IP de salida estática para la consulta de identidad

> **Estado: NO APLICADO.** Este documento y el script que lo acompaña
> (`scripts/setup-identity-static-egress.sh`) son preparación para cuando se
> apruebe explícitamente `USER_APPROVED_PHASE_005_5` (o una fase posterior).
> Nadie debe ejecutar el script contra el proyecto de producción sin esa
> aprobación explícita.

## Por qué hace falta

Cloud Run, por omisión, sale a internet por IPs dinámicas administradas por
Google: cambian sin aviso y no se pueden listar de antemano. Peru API —el
proveedor primario de la consulta de DNI/RUC— puede exigir una lista blanca de
IPs de origen para las cuentas de producción (el plan de desarrollo local ya
se topó con este bloqueo; ver `app/services/identity_providers.py`, el aviso
de verificación pendiente). Sin una IP de salida estática y conocida de
antemano, no hay nada que darle de alta en esa lista blanca.

Decolecta, el proveedor secundario, no tiene esta restricción hasta donde se
sabe. Este runbook solo es indispensable si Peru API confirma el requisito en
producción.

## Arquitectura

```
Cloud Run (bgreda-api)
   │  Direct VPC Egress
   ▼
Subred dedicada (10.x.x.x/28, misma región)
   │
   ▼
Cloud Router (con NAT configurado)
   │
   ▼
Cloud NAT
   │
   ▼
IP externa estática reservada  ──────▶  Peru API (allowlist)
```

Puntos deliberados:

- **Direct VPC Egress**, no el conector de VPC Access clásico: menos piezas,
  sin instancias de conector que mantener, y es el camino recomendado por
  Google para servicios nuevos.
- **Una sola IP estática**, no un pool: el volumen esperado (decenas de
  consultas diarias) no justifica más de una.
- El resto del tráfico saliente del servicio (Supabase, etc.) también pasará
  por esta ruta una vez activado Direct VPC Egress — no hay manera de limitar
  el NAT a un solo destino por IP de origen. Antes de aplicar esto en
  producción, confirmar que ningún otro proveedor externo dependa de ver la IP
  dinámica actual.

## Prerrequisitos

- `gcloud` autenticado contra el proyecto real (`cotizador-greda` en el
  despliegue actual), con permisos de administrador de red (`roles/compute.networkAdmin`
  o equivalente).
- Confirmar la región del servicio desplegado (`southamerica-west1` en el
  despliegue actual) — la subred, el router y el NAT deben vivir en la misma
  región.
- Leer completo `scripts/setup-identity-static-egress.sh` antes de correrlo:
  no asume valores por omisión peligrosos, pero sí asume que quien lo ejecuta
  entiende cada paso.

## Pasos (lo que hace el script, en orden)

1. **Reservar la IP externa estática.**
   ```bash
   gcloud compute addresses create bgreda-identity-nat-ip \
     --project="$PROJECT" --region="$REGION"
   ```
   Anotar la IP resultante: es el dato que se entrega a Peru API para su lista
   blanca.

2. **Crear la subred dedicada** para Direct VPC Egress (si no existe una
   subred de aplicación ya reservada para esto).
   ```bash
   gcloud compute networks subnets create bgreda-egress-subnet \
     --project="$PROJECT" --region="$REGION" --network="$VPC_NETWORK" \
     --range=10.10.10.0/28
   ```

3. **Crear el Cloud Router.**
   ```bash
   gcloud compute routers create bgreda-identity-router \
     --project="$PROJECT" --region="$REGION" --network="$VPC_NETWORK"
   ```

4. **Crear el Cloud NAT**, apuntando la IP reservada en el paso 1 y limitando
   el NAT a la subred del paso 2 (no a toda la VPC: otros servicios en la
   misma red no deben empezar a salir por esta IP sin que alguien lo decida
   explícitamente).
   ```bash
   gcloud compute routers nats create bgreda-identity-nat \
     --project="$PROJECT" --region="$REGION" --router=bgreda-identity-router \
     --nat-external-ip-pool=bgreda-identity-nat-ip \
     --nat-custom-subnet-ip-ranges=bgreda-egress-subnet
   ```

5. **Actualizar el servicio Cloud Run** para salir por esa subred vía Direct
   VPC Egress, con todo el tráfico (`all-traffic`), no solo el destinado a
   rangos privados:
   ```bash
   gcloud run services update bgreda-api \
     --project="$PROJECT" --region="$REGION" \
     --network="$VPC_NETWORK" --subnet=bgreda-egress-subnet \
     --vpc-egress=all-traffic
   ```
   Esto crea una **revisión nueva**. Desplegar primero sin enrutar tráfico
   (`--no-traffic`) y mover el tráfico solo después de confirmar el paso de
   verificación siguiente, siguiendo el mismo patrón de despliegue gradual ya
   usado en Fase 005.

## Verificación posterior

- Desde la revisión nueva (aún sin tráfico o con tráfico mínimo), hacer que el
  servicio llame a un endpoint que devuelva la IP de origen observada (por
  ejemplo, un servicio propio de eco, o el primer intento real contra Peru API
  ya con la IP dada de alta) y confirmar que coincide con la IP reservada en
  el paso 1.
- Confirmar que las conexiones existentes hacia Supabase siguen funcionando
  igual (health check `/ready` en `ok` para los tres componentes) antes de
  mover el 100% del tráfico.
- Solo entonces enrutar el tráfico completo a la revisión nueva.

## Costo

Cloud NAT y la IP estática reservada tienen costo fijo mientras existan,
independiente del volumen de consultas. Es marginal frente al resto de la
infraestructura ya desplegada, pero no es cero: no crear estos recursos hasta
que Peru API confirme que realmente exige la lista blanca en producción.

## Reversión

Todo lo creado aquí se puede deshacer sin tocar datos de aplicación:

```bash
gcloud run services update bgreda-api --project="$PROJECT" --region="$REGION" \
  --vpc-egress=private-ranges-only --clear-network
gcloud compute routers nats delete bgreda-identity-nat \
  --project="$PROJECT" --region="$REGION" --router=bgreda-identity-router
gcloud compute routers delete bgreda-identity-router \
  --project="$PROJECT" --region="$REGION"
gcloud compute networks subnets delete bgreda-egress-subnet \
  --project="$PROJECT" --region="$REGION"
gcloud compute addresses delete bgreda-identity-nat-ip \
  --project="$PROJECT" --region="$REGION"
```

Revertir el servicio de Cloud Run primero, antes de borrar la infraestructura
de red de la que depende — en el orden inverso, el servicio quedaría sin poder
salir a internet hasta el siguiente despliegue.
