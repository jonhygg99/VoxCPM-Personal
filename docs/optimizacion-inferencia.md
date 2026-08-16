# Optimización de inferencia del servidor VoxCPM2

**Fecha:** 2026-07-17 (mediciones originales) · **Actualizado:** 2026-08-16 (batch 2, pipeline QA/generación y mediciones de arranque/saturación — secciones nuevas abajo)
**Hardware:** RTX 3070 8GB, Windows 11, torch 2.11.0+cu128
**Restricción:** la calidad no cambia — todos los cambios activos por defecto son **bit-exact** (con la misma seed, el WAV sale byte-idéntico al código anterior).

> **Estado actual (2026-08-16):** con `--optimize` + batch 2 (default) el mismo benchmark da
> **7.57 s/bloque (ratio 1.04×)** y el flujo completo con QA baja **2.40×** (412.4 → 171.7 s por
> 20 bloques). Resumen ejecutivo de toda la sesión: `docs/resumen-optimizacion-2026-08-16.md`.
> Las tablas de esta sección son la medición original del 2026-07-17.

## Resultado

Sobre el guion fijo `benchmarks/bench_script.txt` (8 bloques medidos + 2 warmup, seed 1234):

| Estado | Inferencia media/bloque | Ratio inferencia/audio |
|---|---|---|
| Código anterior (baseline) | 17.10s | 2.30× |
| **Código actual (por defecto, bit-exact)** | **15.29s** | **2.06×** |
| **Código actual + `--optimize` (triton-windows)** | **8.76s** | **1.24×** |

*Tabla de la medición original (2026-07-17). Con el batch 2 como default de `--optimize`
(2026-08-16): 7.57 s/bloque, ratio 1.04× — ver punto 6.*

- Por defecto: ~11% más rápido, **bit-exact** (8/8 hashes idénticos), y sin la degradación progresiva que acababa en OOM.
- Con `--optimize` (tras instalar `triton-windows`, extra `compile` del pyproject): **43% más rápido adicional** — casi tiempo real. No es bit-exact respecto a eager (equivale a cambiar de seed: misma calidad en distribución, duraciones a <5% de eager, 0 anomalías), pero sí es **reproducible**: misma seed → mismo WAV entre pasadas y arranques compilados (verificado 8/8). Coste: el primer arranque (caché inductor vacía) tarda ~82 s compilando; con caché caliente, 40–44 s (ver 'Coste de arranque'). `--optimize` activa solo `VOXCPM_STOP_CHECK_BATCH=2` (stop-check diferido, bit-exact) para que los CUDA graphs rindan; 0 cudagraph skips y 1 recompile en los logs; VRAM estable (reserved ~6.4 GB plano). El valor 2 viene de un barrrido en la RTX 3070 (2026-08-16): óptimo 1–2 a 7.57 s/bloque (+6.1% vs el 8 anterior, todos bit-exact); 16 pierde 10.4% (ver punto 6).

## Qué se cambió (por defecto, bit-exact)

1. **Tensores de posición persistentes en el bucle AR** (`voxcpm2.py`): antes se creaban 2 `torch.tensor([...], device=...)` (copias H2D bloqueantes) por patch; ahora un tensor por LM avanzado in-place con `add_(1)`. La mayor ganancia.
2. **Máscara de atención izada** (`minicpm4/model.py`): se construía con `torch.arange(8192)` por capa y por paso; ahora una vez por paso desde un buffer preallocated.
3. **Sin `zero_()` del KV cache completo** (`minicpm4/cache.py`): las posiciones rancias quedan siempre excluidas por la máscara.
4. **`torch.cuda.empty_cache()` tras cada petición** (`voxcpm_server.py`): el caching allocator retenía el pico (~7.4 de 8 GB) para siempre → spill de WDDM progresivo (2.3×→3.3× y un servidor de horas a 4.8×) y OOM tras ~15 peticiones variadas. Ahora reserved vuelve a ~5.8 GB por petición y 20+ peticiones seguidas van estables. Ver anexo en `investigacion-ram-servidor.md`.
5. **HTTP keep-alive** (`voxcpm_client.py` + servidor en HTTP/1.1): conexión persistente por hilo con reintento si quedó obsoleta.
6. **Stop-check por lotes** (`VOXCPM_STOP_CHECK_BATCH`, default 1 en eager): infraestructura para diferir el sync GPU→CPU del stop-check; bit-exact vía recorte. Con `--optimize` el servidor lo fija en **2** — barrrido completo en la RTX 3070 (2026-08-16, seed 1234, 8 bloques): batch 1–2 a 7.57 s/bloque (ratio 1.04×, +6.1% vs el 8 usado antes), 3 → 7.67 s, 4 → 7.74 s, 6 → 7.89 s, 8 → 8.06 s, 12 → 8.14 s, 16 → 8.89 s (−10.4%). **Todos los valores salen bit-exact (8/8 hashes)** porque el recorte es exacto: el batch solo decide cuántos patches se generan de más antes de recortar (overshoot). Confirmado en sesión larga de 20 bloques con QA: 7.47 vs 8.06 s/bloque y VRAM plana (reserved 6358–6362 MiB) sin degradación. En eager K>1 pierde ~1.4% por patches descartados, por eso solo se aplica con `--optimize`.

## Qué se probó y se DESCARTÓ (medido, no especulado)

| Palanca | Resultado | Evidencia |
|---|---|---|
| Ventana de atención KV (slice a posiciones válidas) | ~12% MÁS LENTA (slice no contiguo → kernel SDPA peor) y no bit-exact | `bench_kvwindow.json` |
| `cudnn.benchmark` | Contraproducente: re-autotunea con cada longitud nueva del VAE | `bench_optin_flags.json` (paquete −36%) |
| TF32 | Solo tocaría el decode fp32 del VAE (~1.5% del tiempo); no compensa perder bit-exactness | perfil `VOXCPM_PROFILE` |
| `--optimize` SIN triton instalado | No compila nada ("torch.compile disabled - triton is not installed"): queda solo el warmup — minutos de arranque, ~1.5% más lento y cambia los numéricos. Con `triton-windows` instalado sí compila y gana 43% (ver arriba) | `bench_fase5_compile.json`, `bench_triton_compile.json` |

## Pipeline QA/generación (cliente, 2026-08-16)

Fuera del servidor: el flujo de generación con control de calidad (`generate_script.py`,
`generate_shorts.py`, heredado por `watch_generate.py`) serializaba generar → validar
(whisper en CPU) → siguiente bloque, dejando la **GPU ociosa durante el QA**.

Medido en la RTX 3070 (faster-whisper `small`, int8, CPU, 10 bloques de `bench_script.txt`):

| | Por bloque |
|---|---|
| QA (whisper CPU) | 4.20 s (rango 3.82–4.69) — ~58% extra de wall-clock |
| Sin pipeline (generar + QA serial) | 12.71 s — 127.1 s total |
| **Con pipeline** | **~8 s — 86.3 s total en el flujo watchdog completo** (incl. carga QA + concat) |

**Resultado: ~30% menos de tiempo de sesión**, sin cambiar ningún WAV ni los veredictos
del QA (10/10 OK en ambas configuraciones, 0 reintentos).

Cómo funciona: `GenerationPipeline` (`tts_workflow.py`) pide al servidor el bloque
siguiente (GPU) desde un hilo mientras el hilo principal valida el actual (QA en CPU).
Como el QA (4.2 s) es más corto que una generación (~8 s), queda oculto detrás de la
siguiente generación. Solo cubre el primer intento (seed None); los reintentos por QA se
generan de forma síncrona con seed nueva (reintentos y cuarentena intactos), y si el
worker falla por red los bloques restantes vuelven al modo síncrono reportando su propio
error. Solo se activa con QA activo y bloques nuevos: con `--no-validate` o todo
reutilizado no cambia nada (verificado con una 2ª pasada del watchdog sobre el mismo
script: 0 bloques regenerados en 3.6 s). Verificado con la suite completa (118 tests,
incl. 3 nuevos para el pipeline).

## Dónde se va el tiempo (perfil con `VOXCPM_PROFILE=1`)

Por petición (primera petición, servidor fresco): **DiT/difusión ~64%** (10-12 pasos Euler × forward 2×batch por patch), pasos LM ~27%, feat_encoder ~5%, prefill ~4%, VAE decode ~1.5%, sync del stop-check ~0.1%.

Implicación: el techo de mejora adicional estaba en la DiT — exactamente lo que torch.compile + CUDA graphs atacó al instalarse `triton-windows` (43% medido). Ganancias mayores ya exigirían tocar calidad (menos timesteps) o hardware.

### Saturación de GPU (2026-08-16) — el límite del hardware

Muestreo de `nvidia-smi` (cada 200 ms) durante una generación en estado estacionario
(bloque de 128 caracteres, `--optimize` + batch 2): **utilización SM 96–100% constante**
durante todo el bloque, y perfil `VOXCPM_PROFILE=1` con GPU ocupada ≈ wall (6.90 s de
CUDA events frente a 6.91 s de wall): **no hay cuello de CPU/lanzamiento**.

Dos matices frente al perfil de arriba:
- En bloques típicos (128–138 caracteres → ~6.7 s de audio) el **LM domina (~80% del
tiempo GPU**: `base_lm_step` 4.81 s + `residual_lm_step` 0.69 s en 41 pasos), no la DiT
(~15%, 1.05 s en 42 llamadas). El "DiT ~64%" de arriba corresponde a la primera
petición con servidor fresco y otro texto.
- Pico de VRAM de **7983 MiB (97.5% de 8 GB)** durante el decode fp32 del VAE: el
`empty_cache()` por petición y el batch 2 son imprescindibles (un batch multi-bloque
sería OOM en el pico).

Conclusión: el servidor está en el **límite del hardware** (GPU saturada al 100%, VRAM
al 97.5% en el pico). `TORCHINDUCTOR_MAX_AUTOTUNE=1` en LMs + DiT dio 0% de ganancia
(7.58 vs 7.57 s/bloque, a costa de +3.5 min de arranque y numerics distintos) — no queda
ninguna palanca de software sin tocar calidad (timesteps/CFG) o hardware.

## Coste de arranque (referencia operativa, 2026-08-16)

Medido con el flujo de producción real (server `--optimize` + batch 2 con caché
inductor caliente + `watch_generate.py --once` sobre un script de 1 bloque en
`Generate`): **~63 s desde que se lanza el server hasta el primer WAV** (~60 s si los
dos `.bat` se lanzan a la vez: el watchdog hace polling hasta que el server responde).

| Hito | Δ acumulado |
|---|---|
| Lanzar el server (`--optimize`) | 0 s |
| "Servidor listo" (carga + warmup compile) | 40 s |
| Watchdog escanea (QA cargado, ~1.2 s) | 43 s |
| Primer request al server | ~44 s |
| **Primer WAV guardado** (`bloque_001.wav`) | **63 s** |
| `_completo.wav` + SRT | 68 s |

Desglose de los ~20 s del watchdog hasta el primer WAV:

| Fase | Tiempo |
|---|---|
| Carga del modelo QA (faster-whisper `small`, CPU) | ~1.2 s |
| Scan + plan + check server + setup del pipeline | ~1 s |
| **Primer request al server** (`cache_hit: false`) | **14.9 s** (build del cache de voz 4.5 s + inferencia 10.4 s) |
| QA del primer bloque | ~4.2 s (expuesto: no hay bloque siguiente que solapar) |

**El primer bloque cuesta ~2× el estado estacionario (14.9 s vs 7.57 s):** el cache de
voz del prompt (david.wav, 41 s) se construye una sola vez por sesión del server
(`cache_seconds` 4.5, `cache_hit: false`) y la inferencia arranca algo más fría
(10.4 s). Desde el bloque 2 todo vuelve a 7.57 s/bloque con `cache_hit: true` — es un
coste único de ~7 s por sesión, despreciable en sesiones largas. En eager el coste
era aún mayor: la primera petición media ~33 s (build del cache ~19 s sin el
`feat_encoder` compilado + inferencia fría).

Notas operativas:
- VRAM durante el build del cache de voz: pico `cuda_reserved_mb` 6392 MiB (vs 6358–6362
estable) — sin riesgo de OOM.
- Primer arranque con caché inductor fría (sin compilar antes): ~82 s hasta "Servidor
listo" en vez de 40 s. Con caché caliente (la normal tras usar el flujo una vez), 40–44 s.
- Implicación práctica: mantener el server vivo entre sesiones cortas amortiza el
arranque de 40 s y el primer bloque frío; reiniciarlo por sesión cuesta ~63 s + el
primer bloque 2×.

### Precarga del cache de voz en el arranque (evaluado 2026-08-16) — descartada

Se evaluó precargar el cache de voz en el init del server (una petición warmup al
arranque) para evitar los 4.5 s del build y la inferencia fría del primer bloque:
**no merece la pena — es un lavado (~0 neto)** y en el flujo normal (server +
watchdog lanzados juntos) no gana nada:

- **El build de 4.5 s está en el camino crítico igualmente**: precargarlo solo lo
mueve de "primera petición" a "arranque"; el total hasta el primer WAV no cambia
(63 s → ~61 s, dentro del ruido).
- **La inferencia fría (~2.8 s sobre el estacionario: 10.4 vs 7.57 s) no se puede
precargar**: es el overhead de primer uso de la GPU (cudagraphs/cuBLAS/allocator) y
se pega a la primera generación que corra, sea real o dummy. Un warmup solo la
absorbe él mismo — el total de la sesión es idéntico (evidencia: warmup #1 = 13.5 s
y bloques siguientes estables en el benchmark).
- **Un warmup completo (cache + generación dummy en init) es peor**: añade ~10 s al
arranque para ahorrar 7.3 s en el primer bloque → ~−3 s netos.
- Además el server **no conoce la voz al arrancar** (no hay flags de voz; la recibe
por payload) y el cache se keyea por huella del wav + prompt: hardcodearla exigiría
flags nuevos y un match exacto de fingerprint (frágil si cambia el wav o el prompt).
- Único caso con beneficio: server ocioso ≥30 s antes del primer script (p. ej.
auto-arranque al boot) — ahí un warmup del lado cliente (el watchdog conoce la voz)
escondería los 14.9 s en tiempo muerto. No aplica al flujo actual.
- La mejora real ya está en uso: mantener el server vivo entre sesiones (el cache en
memoria persiste, `cache_hit: true` desde el bloque 2); el coste frío de ~7 s solo
se paga por reinicio del server, no por script.

### Coste de reinicio (2026-08-16) — el beneficio de no reiniciar

Medido con un ciclo completo de reinicio (caché inductor caliente, voz real
`david.wav`, driver con 2 peticiones): **cada reinicio evitado ahorra ~48 s** = 41 s
de carga del modelo (rango de la sesión: 40–44 s) + ~7 s del primer bloque frío.

| Petición | Wall total | Desglose |
|---|---|---|
| req1 (fresca, `cache_hit: false`) | 10.59 s | build del cache de voz 4.59 s + inferencia 5.97 s |
| req2 (estacionaria, `cache_hit: true`) | 4.69 s | inferencia 4.69 s, cache 0 s |

Matices medidos:
- **El premium del primer bloque es proporcional al bloque, no fijo**: +27% en bloques
cortos (5.97 vs 4.69 s, ~4.6 s de audio) y +37% en largos (10.4 vs 7.57 s) — es el
overhead de primer uso de la GPU escalando con el trabajo real. El build del cache
(4.6 s) sí es fijo, independiente del texto.
- Con el flujo watchdog, el reinicio también retrasa el primer WAV: ~20 s desde el
escaneo en server frío vs ~13 s en server caliente.
- En tus bloques típicos (~7.5 s de audio), el primer bloque sale a 14.9 s vs 7.57 s
estable → +7.3 s por reinicio además de la carga.

Traducción al flujo (5 scripts/día):

| Patrón | Coste |
|---|---|
| Server vivo todo el día (recomendado) | 48 s una sola vez |
| Reiniciar por script (sesión corta) | +48 s por cada script (~240 s/día) |

El reinicio solo merece la pena si el server va a estar ocioso horas o hay que
liberar los ~6.4 GB de VRAM para otra cosa.

### Comparación end-to-end final (2026-08-16) — 2.40×

Mismo guion de 20 bloques con QA (voz real `david.wav`, faster-whisper `small` en
CPU), misma máquina, mismo día, seeds aleatorias (producción), 0 fallidos / 0
cuarentena en ambas configuraciones:

| Sesión | Wall total | Por bloque |
|---|---|---|
| **Baseline original** (eager + QA serial, flujo pre-pipeline) | 412.4 s | 20.43 s (gen 16.08 + QA 4.35) |
| **Actual** (optimize + batch 2 + pipeline) | **171.7 s** | **8.59 s** (QA oculto) |
| **Mejora** | **−58.4% (2.40×)** | −58.0% |

Descomposición: el server aporta −50.6% (16.08 → 7.94 s/bloque, incl. el primer
bloque frío de la sesión) y el pipeline oculta los 4.35 s de QA por bloque (−21% del
wall baseline). El mismo script pasó de 6 min 52 s a 2 min 52 s. Método: mismo script
de N bloques con QA y voz real; el baseline ejecuta el bucle serial (generar →
validar → siguiente bloque) y el actual `generate_script.run` con el pipeline activo;
ambos sobre el mismo server recién cargado y con seeds aleatorias (producción).

## Cómo verificar / reproducir

```powershell
# Terminal A
python voxcpm_server.py            # esperar "Servidor listo"

# Terminal B — benchmark con seed fija y hashes
python benchmark_voxcpm_inference.py --script-file benchmarks\bench_script.txt --limit 8 --warmup-count 2 --seed 1234 --output-json candidato.json

# Comparar contra el baseline commiteado (tiempos + igualdad de WAVs)
python benchmark_voxcpm_inference.py --compare-json bench_fase2_k1.json candidato.json
```

- `--seed N`: el bloque i usa la seed N+i → los WAV son deterministas y comparables por SHA-256 entre runs/versiones.
- `--save-wav-dir dir`: guarda los WAV para escucharlos.
- `VOXCPM_PROFILE=1` en el servidor: imprime el desglose por buckets (CUDA events) por petición.
- Las métricas del servidor incluyen `cuda_alloc_mb` / `cuda_reserved_mb` / `vram_release_seconds` para vigilar la VRAM.

## Nota operativa

El benchmark depende de que la GPU esté libre: dos servidores cargando el modelo a la vez (o un juego/otro proceso pesado) provocan spill de VRAM y los tiempos salen inflados aunque el audio sea idéntico. Comparar siempre con servidor fresco y recién cargado.
