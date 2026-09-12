# HackMTY 2026 — Track Infosys: Propuestas de arquitectura

_Documento de equipo — 2026._

## Contexto rápido

El reto de Infosys pide un agente forense de IA: dado el libro contable de una empresa y solo una pista de que algo está mal, el agente debe investigar, seguir el dinero y probar el esquema de fraude (facturación falsa, empresas fantasma / EFOS, triangulación) — sin acusar a nadie que no pueda respaldar con evidencia. Entregable: código funcionando + demo en vivo de 3 minutos, donde los jueces esconden un esquema nuevo en los datos y el agente lo debe rastrear en pantalla y defender su razonamiento ante una pregunta sorpresa.

Las tres propuestas comparten el mismo modelo de datos base: un **grafo de relaciones** con nodos para empresas/proveedores, personas, cuentas bancarias, facturas y pagos, y aristas como `EMITIÓ_FACTURA`, `PAGÓ`, `ES_DUEÑO_DE`, `COMPARTE_DOMICILIO`, `COMPARTE_TELÉFONO`, `COMPARTE_CUENTA_BANCARIA` y `EN_LISTA_NEGRA` (esta última alimentada directo del listado 69-B del SAT).

---

## Propuesta 1 — Agente investigador nativo en grafos (recomendada)

Un solo agente LLM que usa algoritmos de grafo determinísticos (NetworkX) como herramientas: detección de ciclos para triangulación de pagos, clustering por atributos compartidos (domicilio, teléfono, cuenta bancaria) para redes de empresas fantasma, y centralidad para detectar intermediarios. El agente decide qué consulta correr según la pista inicial, y el subgrafo que extrae **es** la evidencia — cada arista ya trae folio de factura, monto y fecha, así que "regla violada + monto en pesos" sale casi gratis de la estructura de datos. Las ramas del grafo que el agente exploró y descartó (sin ciclo, sin lista negra, pago único sin nada raro) son el registro natural de "leads no perseguidos y por qué".

## Propuesta 2 — Híbrido: scoring con ML/GNN sobre el grafo + LLM que explica

Mismo grafo, pero se agrega una capa de scoring: features de grafo (grado, participación en ciclos, coeficiente de clustering) alimentando XGBoost o una GNN chica, entrenada sobre los patrones etiquetados de AMLSim (IBM, open source). El agente LLM explica y arma el caso alrededor de lo que el modelo marca. Más vistoso en el pitch y permite citar IEEE-CIS/AMLSim de forma más sustantiva, pero es tiempo real de ingeniería de ML que no se recupera en una ventana de hackathon, y un score de caja negra choca con el criterio de "no acuses sin poder probarlo" a menos que también se haga inspeccionable el razonamiento del modelo.

## Propuesta 3 — Equipo multiagente sobre un grafo compartido

Se divide el agente único en roles especializados: uno que concilia ledger/facturas, uno que rastrea flujos bancarios, uno que revisa lista negra/cumplimiento, y uno que redacta el caso final — todos leyendo y escribiendo sobre el mismo grafo. Es la narrativa de demo más llamativa (se puede mostrar en vivo la contribución de cada especialista cuando un juez pregunta "¿por qué concluyeron eso?"), pero son más piezas móviles para que funcionen de forma confiable en 24-36 horas.

---

## Scoring comparativo

Escala 1–5 en cada eje (5 = mejor). En "Consumo de tiempo", 5 = rápido de implementar, para que la lectura sea consistente con las demás columnas.

| Propuesta | Estilismo | Elegancia | Facilidad de implementación | Consumo de tiempo | Total |
|---|---|---|---|---|---|
| 1. Agente + grafo determinístico | 4 | 5 | 5 | 5 | **19/20** |
| 2. Híbrido ML/GNN + LLM | 5 | 4 | 2 | 2 | 13/20 |
| 3. Equipo multiagente | 5 | 3 | 2 | 2 | 12/20 |

**¿Por qué gana la Propuesta 1?** Es la más elegante en el sentido de que la estructura de datos hace el trabajo pesado (el subgrafo extraído ya es la prueba), es la más rápida de tener funcionando de forma confiable, y sigue siendo vistosa para la demo (grafo con el camino iluminado en tiempo real conforme el agente lo encuentra). Las Propuestas 2 y 3 pesan más en "estilo" — una GNN entrenada o un equipo de agentes coordinados suena mejor en el pitch — pero ese estilo se cobra en riesgo de implementación bajo presión de tiempo.

## ¿Hace falta esperar a los datos reales para decidir?

No. Los ejes de estilismo/elegancia/facilidad dependen sobre todo de la forma del problema (que ya conocemos por el brief) y de las habilidades del equipo, no del dataset en sí — ciclos y clustering en NetworkX funcionan igual sobre datos sintéticos de AMLSim que sobre datos reales. Donde sí hay incertidumbre real es en el consumo de tiempo de la Propuesta 2: no se sabe si una GNN entrena bien y a tiempo hasta ver el volumen y la forma real de los datos que genere AMLSim (cuántos nodos, qué tan desbalanceadas están las clases). Recomendación: arrancar con la Propuesta 1 desde ya, y si sobra tiempo, añadir la capa de ML de la Propuesta 2 **encima** del mismo grafo como mejora incremental, en vez de apostar el proyecto completo a que funcione desde el inicio.

## Pendientes

- Confirmar rúbrica de jueces y reglas de entrega con las páginas 1–2 y el final de la página 4 del PDF del track (aún no vistas completas).
- Definir stack de LLM: Ollama local vs. API con free tier, dado que el agente hace muchas llamadas por investigación.
