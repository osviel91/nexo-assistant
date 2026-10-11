# Stage 15B.1-QA: ToolSelector Benchmark Stability

- Estado: metodología corregida; quality gate PASS
- Base: `74513730e0712f6925f0106052bd6a50e78e967c`
- Rama: `main`
- ToolSelector/benchmark: sin cambios funcionales
- Stage 15C: bloqueado

## Conclusión

El benchmark ahora acepta/rechaza por p95 de CPU del proceso (límite estricto de 100 ms) y reporta pared por separado. No se modifica ToolSelector. El quality gate pasó y los resultados finales de esta corrección se registran abajo.

## Estado inicial y preservación

- HEAD exacto: `74513730e0712f6925f0106052bd6a50e78e967c` (`7451373`). `main` figura `ahead 1` respecto a `origin/main`; no se creó commit.
- No hay cambios tracked. El worktree ya contenía 10 archivos sin seguimiento de Stage 15B/15B.1, incluidos los prototipos OAuth/SSH. Se preservaron sin editar ni borrar.
- La implementación se introdujo en `69f9ea9 feat: add deterministic tool selection`; el benchmark se añadió junto con ToolSelector. No hay una versión previa de este benchmark/selector en la historia de esta rama que permita una comparación A/B del mismo test.
- No hay `pytest.ini`, `pyproject.toml` ni `CONTEXT.md` en el checkout. El runner canónico usa `.venv/bin/python -m pytest -q tests --ignore='tests/*.mjs'`.
- No hay configuración `setup.cfg`, `tox.ini`, `conftest.py` ni workflow `.github/workflows` en este checkout; no hay evidencia de una máquina CI dedicada o de sus límites de carga.
- El entorno de la medición usó Python 3.13.3, macOS 27.0.1 arm64, 10 CPUs. `NEXO_TOOL_SELECTOR` y `NEXO_TOOL_SELECTOR_MAX_TOOLS` no estaban definidos; se aplicaron los defaults `enabled=true`, `limit=10`.

## Qué medía el test antes de la corrección

`tests/test_tool_selection.py::ToolSelectionTests::test_catalog_scaling_benchmark` creaba catálogos sintéticos de 10, 50, 100 y 500 herramientas. La creación de `ToolDefinition`, `EffectiveToolSet` y sus copias profundas sucedía antes de iniciar el cronómetro y quedaba excluida.

Cada una de las 30 iteraciones cronometradas incluye la llamada completa a `select_tools` con la consulta fija `Busca información en mi vault`, `limit=10` y defaults de entorno:

- cálculo del tamaño del esquema autorizado mediante `EffectiveToolSet.definitions()` y `json.dumps`;
- normalización/tokenización de consulta, extracción de capacidades, escaneo y clasificación de herramientas, scoring y ordenamiento;
- construcción del `EffectiveToolSet` seleccionado, incluidas sus copias de parámetros;
- serialización y conteo del esquema seleccionado para el resultado.

Por tanto, no es una medición exclusiva del ranking; incluye el trabajo de métricas de esquema que el contrato actual realiza. No mide la resolución previa de autorización/perfil, la construcción del catálogo, la siguiente serialización del provider ni una petición al modelo. Todas las 500 herramientas de la fixture tienen una descripción similar y resultan candidatas; es una carga sintética más homogénea que un catálogo real.

El test tomaba 30 muestras consecutivas, sin warm-up explícito, medía pared con `time.perf_counter`, y comprobaba `p95 < 100 ms`. El índice era `ordered[int(n * .95) - 1]`: para `n=30`, seleccionaba el elemento 28 (índice 27), rango empírico 93.3%, no p95 nearest-rank. El p95 nearest-rank usa `ordered[ceil(.95*n)-1]`, el elemento 29 (índice 28) para 30 muestras. Con tan pocas muestras, cada observación representa 3.33 puntos percentiles y los dos máximos no participaban en esa fórmula.

## Experimentos

La ejecución focal inicial de `.venv/bin/python -m pytest -q tests/test_tool_selection.py::ToolSelectionTests::test_catalog_scaling_benchmark` pasó: `1 passed in 2.15s`. Una repetición más tarde, con snapshot `load_average=(31.51, 32.53, 32.22)` y `cpu_count=10`, falló sólo en el benchmark: median 52.92 ms y valor de umbral del test 109.38 ms para 500 herramientas. Es una reproducción aislada tanto de pass como de fail; no se modificó el test.

Para separar trabajo de pared y CPU se añadió `scripts/diagnose_tool_selector.py`, ejecutable con:

```sh
.venv/bin/python -m scripts.diagnose_tool_selector
```

El script reutiliza la fixture `tool` del test, tamaños y consulta idénticos. Registra construcción del catálogo por separado (fuera del tiempo de selector), una primera llamada después de crear el catálogo, 30 muestras sin warm-up explícito, 10 llamadas de warm-up y 100 muestras estabilizadas. En cada muestra registra pared y `time.process_time`; instrumenta temporalmente `_schema_chars` para medir por separado la serialización en pared y CPU. Resta CPU de serialización de CPU total para estimar el resto de la llamada. La instrumentación se restaura en `finally`; no cambia archivos de la app ni persiste datos.

Resultados de las tres ejecuciones del harness, todas en el mismo host/Python con load average elevado. `p95` usa nearest-rank; la tabla reporta la serie estabilizada de 100 muestras. Min, máximo y desviación estándar se incluyen para mostrar dispersión.

| Herramientas | Load avg / CPUs | Pared min / mediana / p95 / max / stdev (ms) | CPU proceso min / mediana / p95 / max / stdev (ms) |
| ---: | --- | --- | --- |
| 10 | 28.58 / 10 | 0.357 / 0.392 / 0.546 / 0.990 / 0.087 | 0.358 / 0.392 / 0.518 / 0.699 / 0.051 |
| 50 | 28.58 / 10 | 1.439 / 1.600 / 1.933 / 3.715 / 0.262 | 1.440 / 1.598 / 1.798 / 2.439 / 0.135 |
| 100 | 28.58 / 10 | 2.890 / 3.153 / 4.402 / 7.542 / 0.656 | 2.890 / 3.138 / 3.742 / 4.630 / 0.267 |
| 500 | 28.58 / 10 | 14.521 / 18.619 / 33.013 / 49.312 / 6.931 | 14.484 / 16.280 / 20.382 / 41.857 / 3.005 |
| 10 | 29.39 / 10 | 0.387 / 0.405 / 0.576 / 2.992 / 0.281 | 0.388 / 0.406 / 0.510 / 0.947 / 0.066 |
| 50 | 29.39 / 10 | 1.624 / 2.326 / 11.805 / 40.077 / 4.871 | 1.624 / 1.864 / 3.153 / 3.439 / 0.432 |
| 100 | 29.39 / 10 | 3.104 / 3.762 / 5.730 / 10.651 / 1.058 | 3.104 / 3.481 / 4.780 / 6.756 / 0.556 |
| 500 | 29.39 / 10 | 15.752 / 30.674 / 130.589 / 261.310 / 41.102 | 15.658 / 19.220 / 24.143 / 41.855 / 3.448 |
| 10 | 35.12 / 10 | 0.383 / 0.411 / 7.214 / 27.778 / 3.906 | 0.383 / 0.411 / 0.917 / 1.356 / 0.215 |
| 50 | 35.12 / 10 | 1.574 / 1.786 / 50.092 / 106.537 / 19.351 | 1.575 / 1.711 / 3.537 / 4.444 / 0.638 |
| 100 | 35.12 / 10 | 3.136 / 6.108 / 39.213 / 92.314 / 13.906 | 3.124 / 3.749 / 5.289 / 7.217 / 0.806 |
| 500 | 35.12 / 10 | 15.056 / 31.367 / 133.327 / 246.693 / 45.529 | 15.057 / 17.308 / 21.811 / 43.431 / 3.214 |

La tercera ejecución ilustra la sensibilidad al muestreo: para 500 herramientas, en los primeros 30 valores la mediana de pared fue 33.63 ms, p95 nearest-rank 79.77 ms y la fórmula actual del test 72.83 ms; CPU de proceso p95 21.81 ms. En los 100 valores estabilizados de esa misma ejecución, pared p95 fue 133.33 ms y máximo 246.69 ms, mientras que CPU p95 permaneció en 21.81 ms. El test de 30 observaciones habría pasado, aunque el proceso sufrió pausas de pared durante la tanda larga.

En la tercera ejecución, construcción de fixture más `EffectiveToolSet` tomó aproximadamente 0.22, 0.42 y 2.00 ms para 50, 100 y 500 herramientas, respectivamente (10 herramientas tuvo una interrupción de pared y marcó 4.81 ms). Este trabajo está explícitamente fuera del cronómetro del test. La primera llamada medida fue sólo “primera llamada tras construir fixture” en un intérprete ya iniciado; el script no mide startup de proceso ni importación fría de Python. ToolSelector no tiene caché de resultados entre llamadas.

En esa tanda de 500 herramientas, CPU de serialización de ambos esquemas fue mediana 2.91 ms y p95 3.98 ms; CPU del resto de la llamada fue mediana 14.25 ms y p95 18.36 ms. La serialización es trabajo medible pero no explica los picos de 100–260 ms: su tiempo de pared también aumentó durante paradas, mientras su CPU permaneció baja.

La suite completa había fallado tres veces en la etapa anterior con p95 de 243.44, 396.56 y 188.61 ms. No se volvió a ejecutar la suite completa en esta investigación; la comparación usa esos resultados registrados, el test focal aislado y el harness controlado anterior. No hubo cambios funcionales que justificaran repetir toda la suite, y evitar ejecuciones completas redundantes era parte de la solicitud.

## Evaluación

| Orden | Hipótesis/predicción | Resultado |
| ---: | --- | --- |
| 1 | Contención/scheduling: bajo carga, pared tendrá colas largas mientras `process_time` permanece cerca del coste de cómputo | Respaldada con fuerza por load average 28–35 frente a 10 CPUs y la separación pared/CPU, especialmente a 50/500 herramientas. |
| 2 | 30 muestras sin warm-up y un percentil discreto omiten o representan pobremente la cola | Respaldada: en la misma tanda de 500, p95 pared de primeros 30 = 79.77 ms y p95 de 100 = 133.33 ms; la fórmula actual informa 72.83 ms en los primeros 30. |
| 3 | La medición está dominada por serialización del catálogo | Parcialmente refutada: se mide y escala, pero en 500 herramientas su CPU p95 fue 3.98 ms de 21.81 ms de CPU p95 total. Sus tiempos de pared también heredan la desplanificación. |
| 4 | Existe una regresión real del algoritmo que supera 100 ms de cómputo | No respaldada por las medidas CPU; p95 quedó en 18–24 ms. No puede excluirse regresión respecto a un baseline versionado, porque el selector y el test se introdujeron juntos y la corrida Stage 14A no es una comparación controlada. |

**Contención/scheduling: hipótesis principal, fuertemente respaldada.** El load average observado fue 2.9–3.5 veces el número de CPUs. En la muestra de 500 herramientas con p95 de pared 133 ms, p95 de CPU fue 21.8 ms; en 50 herramientas, hubo picos de pared hasta 106 ms con máximo de CPU de sólo 4.4 ms. Eso demuestra que parte sustancial del tiempo añadido ocurre mientras el proceso no consume CPU. Es evidencia de desplanificación/espera del host, no prueba de qué otro proceso concreto causó cada pausa. No se atribuye a macOS como causa intrínseca.

**Muestreo: contribuyente metodológico claro.** 30 observaciones sin warm-up tienen poca resolución de cola y la fórmula actual informa una posición inferior a p95. La tanda de 100 capturó pausas que los primeros 30 no contenían. Añadir warm-up por sí solo no las elimina: en la tercera ejecución, las pausas continuaron tras 10 warm-ups.

**Serialización: incluida pero no causa principal.** `definitions()`/deepcopy/JSON forman parte del tiempo de `select_tools` porque el selector calcula caracteres del esquema autorizado y elegido. La CPU observada es ~4 ms p95 de ~22 ms CPU p95 para 500 herramientas. No debe confundirse esa operación con la serialización posterior del provider, que está fuera del cronómetro.

**Regresión de ToolSelector: no demostrada.** El trabajo escala con el número de herramientas y la ruta de CPU del caso sintético de 500 herramientas quedó muy por debajo de 100 ms p95 en las tres tandas (18.4–24.1 ms aproximadamente, salvo discretización). No hay una revisión de código posterior al commit que introdujo implementación y benchmark juntos ni un baseline A/B reproducible. La documentación Stage 14A registra una corrida previa de 30 muestras con 16.05 ms p95 a 500 herramientas; es del mismo diseño, pero faltan condiciones/CPU del host para tratarla como comparación controlada.

## Metodología anterior y nueva

Anteriormente `test_catalog_scaling_benchmark` medía 30 muestras consecutivas de pared con `perf_counter`, sin warm-up, y aceptaba con `ordered[int(n * .95) - 1]`. Para 30 datos eso selecciona el rango empírico 93.3%, no p95 nearest-rank. Pausas de scheduling del host se confundían con coste computacional.

Ahora mantiene la misma consulta, límite de selección 10 y catálogos sintéticos de 10, 50, 100 y 500 herramientas. Construye cada catálogo antes de medir, ejecuta 10 calentamientos y toma 100 muestras estabilizadas. Cada llamada completa a `select_tools` mide tanto `time.process_time()` como `time.perf_counter()`. La aceptación usa exclusivamente p95 de CPU de proceso < 100 ms. Se reportan mediana y p95 de pared y CPU por separado. p95 usa nearest-rank: `sorted(values)[ceil(0.95*n)-1]` (muestra 95 de 100). No se escoge el mejor resultado de ejecuciones repetidas.

El coste medido incluye normalización, clasificación, scoring/ordenamiento, conteos y serialización de esquemas y construcción del conjunto seleccionado, incluidas copias profundas. La creación inicial del catálogo/fixture permanece fuera del contrato de `select_tools`; el harness diagnóstico informa ese coste separado. La consulta y el catálogo homogéneo preservan la carga de trabajo original para que la corrección no cambie representatividad ni conducta.

El harness `scripts.diagnose_tool_selector` ejecuta la misma ruta, registra CPU y pared y toma snapshot de load average/CPUs. `--inject-cpu-regression` añade 110 ms de trabajo CPU por llamada sólo dentro del proceso temporal del harness, limitado a 500 herramientas, y comprueba que el p95 medido rebasa el límite. No toca archivos de ToolSelector.

## Limitaciones

`time.process_time` mide CPU consumida por el proceso Python, no CPU de hilos/procesos externos. Si el trabajo medido se delega a un subprocess, worker remoto, servicio o ejecución concurrente fuera del proceso, ese coste puede no quedar reflejado; en ese caso el contrato necesita instrumentación de esos workers o una métrica end-to-end adicional. El cronómetro de pared sí expone esperas y scheduling, pero no determina su causa. La construcción del catálogo no forma parte de `select_tools`; se deja fuera de la aserción y se informa separadamente en el diagnóstico. No se modifica este límite implícito del contrato.

El benchmark sintético es una guardia de regresiones del selector, no una garantía de latencia de usuario ni una comparación A/B contra baseline versionado. Las pruebas deterministas existentes siguen protegiendo selección y autorización.

## Verificación final

- Benchmark focal: 3/3 ejecuciones pasaron (`1 passed` en 3.20 s, 3.83 s y 3.53 s). Cada ejecución recorrió los cuatro tamaños, aplicando 10 warm-ups y 100 muestras con aceptación por p95 de CPU < 100 ms.
- Harness normal: load average registrado `21.40, 25.98, 28.79` con 10 CPUs. A 500 herramientas, p95 estabilizado fue 18.185 ms CPU de proceso y 21.133 ms pared; la mediana fue 16.150 ms CPU / 16.927 ms pared. El p95 de pared fluctúa aparte de CPU. La construcción del catálogo fue 4.908 ms y permanece fuera del contrato de `select_tools`, claramente reportada.
- Regresión artificial: `python -m scripts.diagnose_tool_selector --inject-cpu-regression` añadió 110 ms de bucle CPU por llamada en el harness (500 herramientas). Se detectó y rechazó con p95 CPU de proceso 128.828 ms >= 100 ms. La inyección sólo existe en el proceso de diagnóstico; ToolSelector no se editó.
- Quality gate: una ejecución de `./scripts/nexo check` pasó: `252 passed, 1 skipped`, frontend/hygiene incluidos. Persisten 5 warnings deprecación Starlette/FastAPI y 3 avisos Node de `MODULE_TYPELESS_PACKAGE_JSON`; no fueron fallos.
- Limitación: process CPU no cuenta trabajo delegado a otros procesos/servicios ni representa latencia end-to-end. Para proteger esos contratos haría falta medir sus procesos o añadir una medición end-to-end separada. La pared reportada sigue permitiendo observar scheduling sin contaminar la aceptación computacional.
- Sin commit, push ni despliegue. Los cambios pendientes de Stage 15B/15B.1 ajenos a estos archivos se preservan. Stage 15C continúa bloqueada y se detiene aquí para revisión antes de consolidar Stage 15B/15B.1.
