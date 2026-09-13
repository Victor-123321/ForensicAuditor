# Integración de Snowflake — CORPIDE

**Premio objetivo:** Best Use of Snowflake API (Raspberry Pi 4 por integrante)
**Estado:** plan aprobado, pendiente de implementar
**Dueño natural del código:** Aldo (`data/`, `graph/`), pero se ejecuta como un solo prompt sobre `main` post-merge, igual que el de Gemini.

---

## 1. El problema con la versión obvia

La forma fácil de "usar Snowflake" sería mandar una llamada más a un LLM vía Cortex y ya. Eso califica técnicamente pero es transparente: un juez que ve tres proveedores de LLM en el mismo proyecto (Ollama + Gemini + Cortex) sin razón arquitectónica no piensa "qué completo", piensa "esto es un cartón de bingo de patrocinadores". Y nos pega justo en el criterio **Clarity** de la rúbrica de Infosys, que es donde más barato se pierden puntos.

Así que la integración se diseñó al revés: **primero buscamos dónde el proyecto tiene un hueco real, y resulta que Snowflake lo tapa.** Hay dos huecos y son genuinos.

## 2. Hueco #1 — No tenemos historia de escala

Hoy el estate completo (proveedores, facturas, pagos) se carga entero a un `MultiDiGraph` de NetworkX en memoria. Funciona perfecto con 150 facturas. Si un juez pregunta *"¿esto corre en una empresa real con dos millones de facturas?"*, la respuesta honesta ahorita es "no, se muere".

**La solución no es Snowflake por Snowflake, es una arquitectura de dos niveles que resulta ser exactamente para lo que sirve un warehouse:**

- Los detectores **baratos y de alto volumen** son pura lógica de conjuntos: cruce con la lista negra del SAT, descuadre factura-vs-pago, agrupación por atributo compartido (mismo teléfono / misma cuenta bancaria / misma dirección). Eso es `JOIN` y `GROUP BY`. Se bajan a SQL y corren **dentro del warehouse**, sobre el estate completo.
- Los detectores **caros y genuinamente de grafo** —detección de ciclos, betweenness centrality— sí necesitan un grafo, pero ya no necesitan el estate completo: corren solo sobre el **subconjunto sospechoso** que devolvió el SQL.

Con eso, la respuesta a la pregunta del juez cambia a: *"sí, y aquí está por qué — el filtrado pesado es SQL en el warehouse, el grafo solo ve a los cientos de candidatos, no a los millones de registros."* Eso es un argumento de **Feasibility** real, no marketing.

## 3. Hueco #2 — Ningún detector lee lo que dice la factura

Todos nuestros detectores son **estructurales**: montos, fechas, IDs, relaciones. Ninguno lee el campo `concepto` del CFDI.

Y resulta que ahí está la señal más característica del fraude 69-B en la vida real. Las facturas de EFOS describen servicios genéricos e inverificables: *"servicios de consultoría estratégica"*, *"asesoría administrativa diversa"*, *"servicios profesionales varios"*. El concepto legal mexicano es **falta de materialidad** — la operación no tiene sustancia comprobable. Un auditor forense de carne y hueso lee esa línea y sabe que no hay forma de verificar que el servicio existió.

**Ni una query SQL ni un algoritmo de grafo pueden detectar eso.** Requiere entender lenguaje. Ese es el hueco, y es el que justifica Cortex de verdad.

### Por qué Cortex y no Gemini para esto

Es la pregunta que va a hacer el juez, y tiene buena respuesta:

1. **Es una operación masiva por fila.** Clasificar el concepto de cada factura con Gemini significa miles de round-trips desde la app. En Snowflake es una sola query: el LLM corre **al lado de los datos**, dentro del warehouse, sin sacar la información. Ese es literalmente el argumento de arquitectura de Snowflake y en este caso sí aplica.
2. **Protege el presupuesto de NFR-4.** Tenemos un máximo de 2 llamadas a Gemini por investigación (síntesis final + pregunta del juez). La clasificación masiva no consume ninguna de las dos porque no pasa por Gemini.
3. **El resultado entra como un detector más**, devolviendo objetos `Lead` idénticos a los demás, con los IDs de factura que lo respaldan. O sea que **respeta el guardrail de evidencia** (FR-17) igual que todo lo demás — no es un canal paralelo por donde el modelo pueda acusar sin pruebas.

## 4. Lo que decidimos NO hacer

**Cortex Analyst** (text-to-SQL sobre un modelo semántico) se ve tentador para el endpoint `/ask` del juez: le preguntas en lenguaje natural y genera SQL contra el estate. Lo descartamos a propósito por dos razones:

- Compite con el camino de Gemini que acabamos de construir, y duplicar el `/ask` en dos implementaciones a horas del deadline es como se rompen las demos.
- **Rompería el guardrail.** Cortex Analyst genera SQL arbitrario contra las tablas; nuestra garantía de diseño es que el agente solo puede afirmar cosas respaldadas por una arista concreta del `evidence_trail`. Un canal que consulta la base directamente se salta esa garantía, que es justamente nuestro diferenciador en el criterio **Judgment**.

Si alguien del equipo lo propone, esta es la razón por la que no.

## 5. Arquitectura resultante

```
                    ┌─────────────────────────────────────┐
                    │  SNOWFLAKE (warehouse)              │
  estate generado ──►  SUPPLIERS · INVOICES · PAYMENTS    │
  lista negra SAT ──►  SAT_BLACKLIST                      │
                    │                                     │
                    │  Detectores SQL (alto volumen):     │
                    │   · cruce lista negra 69-B          │
                    │   · descuadre factura/pago          │
                    │   · atributos compartidos           │
                    │                                     │
                    │  Detector Cortex (lenguaje):        │
                    │   · concepto vago / sin materialidad│
                    └──────────────┬──────────────────────┘
                                   │  solo el subconjunto sospechoso
                                   ▼
                    ┌─────────────────────────────────────┐
                    │  NetworkX (en la app)               │
                    │   · detección de ciclos             │
                    │   · betweenness centrality          │
                    └──────────────┬──────────────────────┘
                                   ▼
                         agente ReAct (Ollama)
                                   ▼
                      guardrail de evidencia (FR-17)
                                   ▼
                   Gemini: narrativa final + /ask del juez
```

Cada LLM del sistema tiene ahora una razón distinta de existir, y se puede explicar en una frase: **Ollama** razona barato y en volumen porque es local; **Cortex** lee texto masivamente porque corre junto a los datos; **Gemini** redacta y responde al juez porque es el de mayor calidad y son solo 2 llamadas. Eso ya no es bingo, es una decisión.

## 6. Reglas que no se negocian

1. **Todo detrás de un flag.** `DATA_SOURCE=local|snowflake`. Si el día de la demo se cae el wifi, se acaba el trial, o el warehouse no despierta, se cambia a `local` y el proyecto corre exactamente como hoy. **Esta integración nunca puede ser un punto único de falla de la demo.**
2. **`shared/schemas.py` no se toca.** Los detectores SQL devuelven los mismos objetos `Lead` que los detectores locales. Esa es la costura limpia: el agente no se entera de dónde vinieron.
3. **Las pruebas corren sin credenciales de Snowflake.** Si no hay PAT, los tests de Snowflake se saltan (`pytest.mark.skipif`), no fallan.
4. **Se implementa después del prompt de Gemini**, no antes. Gemini cierra un hueco funcional real (`/ask` del juez, criterio Judgment). Snowflake es mejora de arquitectura + premio. Si solo alcanza tiempo para uno, es Gemini.

---

## 7. Paso previo (humano, antes de cualquier código)

**Hazlo primero y confirma que funciona antes de que nadie escriba una línea.** Si la cuenta no levanta, no vale la pena invertir en el código. Presupuesta 30-45 minutos si es la primera vez.

1. Crea el trial de 120 días en https://mlh.link/snowflake-signup (elige región US si te da opción; evita regiones donde Cortex tenga menos modelos).
2. En un worksheet de Snowflake, crea la infraestructura:

```sql
CREATE DATABASE IF NOT EXISTS FORENSIC_AUDITOR;
CREATE SCHEMA IF NOT EXISTS FORENSIC_AUDITOR.ESTATE;
CREATE WAREHOUSE IF NOT EXISTS FORENSIC_WH
  WAREHOUSE_SIZE = 'X-SMALL'
  AUTO_SUSPEND = 600          -- 10 min, no 60: que no se duerma a media demo
  AUTO_RESUME = TRUE;
```

3. Genera un **Programmatic Access Token (PAT)** para tu usuario (Settings → Authentication, o vía `ALTER USER ... ADD PROGRAMMATIC ACCESS TOKEN`). Guárdalo, solo se muestra una vez.
4. Anota tu **account identifier** (formato `ORGNAME-ACCOUNTNAME`). La URL base es `https://<account-identifier>.snowflakecomputing.com`.
5. **Prueba con un curl antes de seguir.** Si esto no responde 200, nada de lo demás va a funcionar:

```bash
curl -X POST "https://<ACCOUNT>.snowflakecomputing.com/api/v2/statements" \
  -H "Authorization: Bearer $SNOWFLAKE_PAT" \
  -H "X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "User-Agent: forensic-auditor/1.0" \
  -d '{"statement":"SELECT 1","timeout":60,"warehouse":"FORENSIC_WH","database":"FORENSIC_AUDITOR","schema":"ESTATE"}'
```

### Trampas conocidas

- **PAT y network policy:** Snowflake frecuentemente exige que el usuario tenga una *network policy* asignada antes de permitir autenticación por PAT. Si el curl regresa 401 con la cuenta recién creada, es casi siempre esto. Se resuelve creando una política permisiva para el hackathon (`CREATE NETWORK POLICY ... ALLOWED_IP_LIST = ('0.0.0.0/0')` y asignándola al usuario) — **sirve para un trial de hackathon, no lo copies a producción jamás.**
- **Warehouse frío:** la primera query después de estar suspendido tarda 5-10 segundos en lo que el warehouse despierta. Corre una query de calentamiento **justo antes de pararte frente a los jueces**.
- **Modelo de Cortex no disponible en tu región:** si la llamada a Cortex falla con un error de modelo no disponible, prueba `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';` o cambia a un modelo que sí esté en tu región.

---

## 8. El prompt

Se corre sobre `main`, **después** del prompt de Gemini.

```
Estoy en la rama main del repo forensic-auditor, ya con las 4 ramas
mergeadas y con la integración de Gemini terminada. Vamos a agregar
Snowflake como capa de datos y como detector de lenguaje.

ANTES DE ESCRIBIR NADA:
1. Corre `pytest -v` y confirma que todo pasa hoy. Si algo falla, arréglalo
   o dímelo antes de seguir.
2. Lee docs/srs-forensic-auditor-agent.md (secciones 2.4, 5.1, 5.2, 5.3),
   docs/snowflake-integracion.md completo, y luego data/generator/,
   graph/builder.py, graph/detectors.py y shared/schemas.py.
3. Confirma que ya existe una cuenta de Snowflake funcionando: debe haber
   SNOWFLAKE_ACCOUNT y SNOWFLAKE_PAT en el entorno y el curl de prueba de
   la sección 7 del doc debe responder 200. Si no, PARA y avísame — no
   escribas código contra una cuenta que no existe.

REGLA QUE GOBIERNA TODO ESTE CAMBIO:
Nada de esto puede romper la demo. Todo va detrás de un flag
DATA_SOURCE=local|snowflake que por default es "local". Con DATA_SOURCE=local
el proyecto debe comportarse EXACTAMENTE como hoy, mismo código de salida,
mismos tests pasando, sin importar si hay credenciales de Snowflake o no.
NO modifiques shared/schemas.py.

PASO 1 — data/snowflake_client.py
Crea un cliente REST delgado, sin el conector pesado de Snowflake (usa
requests). Funciones públicas:

  snowflake_available() -> bool
      True solo si SNOWFLAKE_ACCOUNT y SNOWFLAKE_PAT están en el entorno.

  execute_sql(statement: str, timeout: int = 60) -> list[dict]
      POST https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com/api/v2/statements
      Headers:
        Authorization: Bearer {SNOWFLAKE_PAT}
        X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN
        Content-Type: application/json
        Accept: application/json
        User-Agent: forensic-auditor/1.0
      Body: {"statement":..., "timeout":..., "warehouse": SNOWFLAKE_WAREHOUSE,
             "database": SNOWFLAKE_DATABASE, "schema": SNOWFLAKE_SCHEMA}

      La respuesta trae resultSetMetaData.rowType (nombres de columna) y
      data (arreglo de arreglos, TODOS los valores vienen como string).
      Mapea eso a una lista de dicts con los nombres de columna, y castea
      a número las columnas que el rowType marque como fixed/real.

      IMPORTANTE: si la respuesta es 202 en vez de 200, la query es asíncrona
      y trae un statementHandle — haz polling a
      GET /api/v2/statements/{handle} hasta que devuelva 200, con un límite
      de reintentos. Con nuestro volumen casi siempre será 200, pero
      manéjalo.

  cortex_complete(prompt: str, model: str = "claude-3-5-sonnet") -> str
      POST .../api/v2/cortex/inference:complete
      Mismos headers de auth.
      Body: {"model": model, "messages":[{"role":"user","content":prompt}],
             "stream": false}
      Verifica si la respuesta viene como JSON plano o como SSE y maneja
      ambos casos; extrae el texto de la respuesta.

Toda función debe lanzar una excepción clara y tipada si falla, nunca
regresar basura silenciosa.

PASO 2 — data/snowflake_loader.py
  ensure_schema()
      Crea si no existen las tablas en FORENSIC_AUDITOR.ESTATE:
        SUPPLIERS(supplier_id, name, rfc, phone, address, bank_account,
                  incorporation_date)
        INVOICES(invoice_id, supplier_id, issue_date, amount, concepto,
                 uuid_cfdi)
        PAYMENTS(payment_id, invoice_id, supplier_id, payment_date, amount,
                 bank_account)
        SAT_BLACKLIST(rfc, name, status, publication_date)

  load_estate(estate)
      Toma el objeto de estate que ya produce data/generator/ y lo inserta.
      Usa INSERT multi-fila por lotes (por ejemplo 200 filas por statement),
      no una llamada por fila — serían cientos de round-trips. Haz TRUNCATE
      antes de cargar para que regenerar el estate no duplique.

  load_sat_blacklist(df)
      Carga el CSV real del SAT. Respeta la columna de status tal como viene
      ("Definitivo", "Presunto", "Desvirtuado", "Sentencia Favorable") — NO
      la filtres aquí, el filtro va en el detector.

PASO 3 — graph/sql_detectors.py
Tres detectores que corren como SQL en el warehouse y devuelven EXACTAMENTE
los mismos objetos Lead que graph/detectors.py (mismo schema, mismos campos
de evidencia). Revisa la firma real de Lead en shared/schemas.py y cálcala.

  detect_blacklisted_suppliers_sql()
      JOIN de SUPPLIERS con SAT_BLACKLIST por RFC normalizado
      (UPPER(TRIM(...)) de los dos lados).
      FILTRO CRÍTICO: solo status IN ('Definitivo','Presunto').
      Un proveedor con status 'Desvirtuado' fue EXONERADO por el SAT —
      acusarlo sería un falso positivo grave y es justo el tipo de error que
      el criterio Judgment castiga. Deja un comentario en el código
      explicando esto.

  detect_invoice_payment_mismatch_sql()
      LEFT JOIN de INVOICES con PAYMENTS agregando por factura, HAVING sobre
      la diferencia absoluta mayor a 0.01.

  detect_shared_attributes_sql()
      GROUP BY phone / address / bank_account por separado (UNION ALL),
      HAVING COUNT(*) > 1, con ARRAY_AGG de los supplier_id del grupo.
      Ignora nulos y cadenas vacías.

PASO 4 — Detector de concepto vago con Cortex
En el mismo graph/sql_detectors.py:

  detect_vague_concepts_cortex()
      Este es el detector que ni SQL ni el grafo pueden hacer: clasifica el
      texto del campo concepto de las facturas.

      OPTIMIZACIÓN OBLIGATORIA: clasifica SELECT DISTINCT concepto FROM
      INVOICES, no todas las filas. En nuestro estate hay ~150 facturas pero
      probablemente 15-25 conceptos distintos. Clasificas los distintos y
      haces join de regreso. No desperdicies llamadas.

      Intenta primero hacerlo en una sola query usando la función SQL de
      Cortex (SNOWFLAKE.CORTEX.COMPLETE o AI_COMPLETE según lo que soporte
      la cuenta — VERIFICA cuál existe corriendo una query de prueba, no
      asumas). El prompt al modelo debe ser en español, pedirle que responda
      con UNA sola palabra, VAGO o ESPECIFICO, y explicarle el criterio:
      un concepto es VAGO si describe un servicio genérico e inverificable
      sin entregable concreto ("servicios de consultoría", "asesoría
      administrativa diversa"), y ESPECIFICO si nombra un bien, cantidad,
      periodo o entregable identificable.

      Si la función SQL de Cortex no está disponible en la cuenta, cae al
      plan B: trae los conceptos distintos con execute_sql y clasifícalos
      con cortex_complete() del Paso 1, en lotes.

      Devuelve un Lead por cada proveedor que tenga facturas con concepto
      VAGO, con los invoice_id que lo respaldan en el campo de evidencia.
      El Lead debe entrar al mismo pipeline que los demás y quedar sujeto al
      guardrail — no es un canal privilegiado.

PASO 5 — Cableado con el flag
En donde hoy se construye el grafo y se corren los detectores:
  - Si DATA_SOURCE=local: comportamiento actual, sin cambios, sin tocar
    Snowflake.
  - Si DATA_SOURCE=snowflake: corre los 4 detectores de los Pasos 3-4 contra
    el warehouse, junta los IDs de entidades sospechosas que devolvieron, y
    construye el grafo de NetworkX SOLO con ese subconjunto y sus vecinos
    directos. Sobre ese grafo reducido corre los detectores que sí necesitan
    grafo (ciclos y betweenness), que se quedan en Python sin cambios.

  Si DATA_SOURCE=snowflake pero Snowflake falla por lo que sea, registra un
  warning claro y CAE A MODO LOCAL en vez de tronar. La demo nunca se cae
  por esto.

PASO 6 — Inyección de escenarios en modo Snowflake
El Scenario Injector deja que un juez inyecte un esquema de fraude nuevo en
vivo. En modo snowflake eso ahora significa INSERTs al warehouse y volver a
correr los detectores SQL. Haz que funcione y mide cuánto tarda — si pasa de
unos 10 segundos, dime, porque eso cambia el guion de la demo.

PASO 7 — Pruebas
  - tests/test_sql_detectors.py con @pytest.mark.skipif(not
    snowflake_available()): carga un estate chico, corre los 4 detectores y
    verifica que devuelvan Lead bien formados.
  - Una prueba que SÍ corre siempre: con DATA_SOURCE=local y sin
    credenciales, el flujo completo funciona idéntico a hoy.
  - Una prueba de que status='Desvirtuado' NO genera acusación.

AL FINAL
Corre `pytest -v` completo. Prueba a mano los dos modos: DATA_SOURCE=local
sin credenciales, y DATA_SOURCE=snowflake con ellas. Enséñame `git diff
--stat` y prepara un commit. Dime también cuánto tardó una investigación
completa en cada modo.
```

---

## 9. Qué decimos en la demo

Dos frases, ni una más, cuando se muestre el grafo:

> *"Los detectores baratos corren como SQL dentro de Snowflake sobre el estate completo — solo el subconjunto sospechoso entra al grafo. Por eso esto escala a una empresa con millones de facturas y no solo a nuestra demo."*

> *"Y hay una señal que ni el SQL ni el grafo pueden ver: lo que dice la factura. 'Servicios de consultoría diversos' es el lenguaje de una empresa fantasma, y eso lo clasifica Cortex dentro del warehouse, sin sacar los datos de ahí."*

---

## Fuentes

- [Cortex REST API — Snowflake Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-rest-api)
- [Snowflake-Labs/cortex-rest-api-demo](https://github.com/Snowflake-Labs/cortex-rest-api-demo)
- [Submitting a request to execute SQL — SQL API](https://docs.snowflake.com/en/developer-guide/sql-api/submitting-requests)
- [Authenticating Snowflake REST APIs](https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/authentication)
- [Using programmatic access tokens for authentication](https://docs.snowflake.com/en/user-guide/programmatic-access-tokens)
- [Cortex Analyst REST API](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api) (evaluado y descartado, ver sección 4)
