# Guion de demo — 3 minutos

Una sola pestaña, `http://localhost:8000/`. Nada de terminal en pantalla.

## Antes de empezar (5 minutos antes, no en vivo)

```bash
python -m scripts.check_ollama        # el LED tiene que quedar verde
uvicorn api.main:app --port 8000
```

- Abre el dashboard y confirma que el LED del modelo late en **verde**.
  Si está ámbar o rojo, arréglalo ahora: en vivo no hay tiempo.
- Deja el grafo **vacío**. El escenario se inyecta delante del jurado —
  es la mitad del argumento.
- Ventana maximizada, zoom al 100%.

## El guion

| Tiempo | Qué haces | Qué dices |
|---|---|---|
| **0:00** | Dashboard quieto, sello `SIN ABRIR`. | "Una empresa mexicana con miles de facturas. El fraude no está en una factura: está en la relación entre varias. Eso es lo que no ve un dashboard normal." |
| **0:20** | **Inyectar escenario** → **Facturación falsa**. | "Le voy a meter un fraude real, del tipo que persigue el SAT: un proveedor en la lista negra del artículo 69-B facturando servicios que nunca se prestaron." |
| **0:40** | El grafo aparece y se llena. | "Este es el patrimonio completo. Nadie le dijo al agente dónde buscar." |
| **0:50** | **Investigar**. No toques nada más. | "A partir de aquí no hay guion. El modelo corre local, en una laptop de este cuarto." |
| **1:00** | Deja correr la traza. | "Lean los pasos: razona, llama una herramienta, observa lo que le devuelve el grafo." |
| **1:45** | **El momento clave.** Al final de la corrida aparecen pasos ámbar *Pista descartada* que empiezan con `Dropped accusation against…`. Señala uno. | "Y aquí está lo importante. Esa línea es una acusación que **el propio modelo escribió** y que el sistema **borró**, porque citó una arista que no existe en el grafo. El modelo no tiene la última palabra." |
| **2:10** | El grafo resalta el rastro; rojo sobre las aristas citadas. | "Lo que queda en rojo son las aristas que sí sostienen la acusación. Cada una existe, con su ID." |
| **2:25** | **Expediente**. | "Este es el expediente: solo lo que sobrevivió." |
| **2:45** | Señala el monto, en monoespaciado. | "Y el monto no es lo que dijo el modelo. Es la suma de lo que quedó probado." |
| **2:55** | Cierra ahí. | "Un agente que acusa sin pruebas no sirve para nada. Este no puede." |

## Detalles que te pueden morder

- Las rechazadas por el guardrail salen **en inglés** (`Dropped
  accusation against RFC…`) y se ven igual que las pistas que el modelo
  descartó por su cuenta. Distínguelas por el texto, y tradúcelas tú al
  narrar.
- Si la corrida **no rechaza nada**, no inventes: pasa directo al
  expediente y apóyate en el monto y en las aristas rojas.
- La tarjeta *Pistas no perseguidas* del expediente es otra cosa: son
  las pistas que el modelo decidió no seguir, no las que el sistema le
  borró. Sirve, pero no es el argumento.

## Si algo se rompe

- **El modelo no responde** → el expediente sale vacío y la UI lo dice:
  *"La investigación no llegó a correr"*. **No lo presentes como
  inocencia.** Di "se cayó el modelo", cambia a **Archivo** y abre un
  caso anterior: los expedientes guardados se leen sin grafo.
- **Sale "Sin fraude probado" con rastro** → eso **sí** es un resultado.
  Véndelo: "recorrió el grafo y no encontró nada que pudiera probar. Un
  auditor que no inventa vale más que uno que siempre encuentra algo."
- **Se traba a media corrida** → **Detener**, y otra vez **Investigar**.
  El 409 está manejado; no se rompe nada.

## Lo que no debes hacer

- No expliques la arquitectura. Nadie la pidió.
- No leas la traza completa en voz alta — el jurado ya la está leyendo.
- No abras **Ajustes** en vivo.
