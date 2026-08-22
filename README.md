# BGreda

Backend del **Cotizador Greda** — API HTTP construida con FastAPI.

Este repositorio contiene exclusivamente el backend. Es la **única autoridad** del
sistema: toda la lógica de negocio, la autenticación, el acceso a Supabase /
PostgreSQL y la integración con servicios externos viven aquí.

- Frontend (consumidor de esta API): <https://github.com/Anthgg/FGreda>

## Regla arquitectónica

```
React (FGreda)
  |  HTTPS
FastAPI (BGreda)
  |
Supabase / PostgreSQL / servicios externos
```

El frontend **nunca** habla directamente con Supabase ni con PostgreSQL.

## Estado

Commit técnico de bootstrap. La implementación de la Fase 1 se desarrolla en la
rama `feat/phase-001-foundation-auth`.
