# Resumen ejecutivo — optimización de inferencia (sesión 2026-08-16)

**Hardware:** RTX 3070 8GB, Windows 11, venv `venv_voxcpm` (torch 2.11.0+cu128 + triton-windows 3.7.1).
**Flujo de producción:** `iniciar_voxcpm_server.bat` (server `--optimize` en 127.0.0.1:8765) + `watch_generate.bat` (voz `voices/david.wav`, QA faster-whisper `small` int8 en CPU).
**Restricción del proyecto:** no tocar calidad. Todo lo aplicado es bit-exact o "misma calidad en distribución" (como ya hizo `--optimize`).

## Mejoras aplicadas (con números medidos en esta máquina)

| Mejora | Antes | Después | Ganancia | Verificación |
|---|---|---|---|---|
| **`--optimize`** (torch.compile + CUDA graphs) | 14.76 s/bloque (eager, ratio 1.99×) | 8.06 s/bloque (ratio 1.10×) | **−45.4%** | B1/B2/B3 reproducibles 8/8 hashes entre runs y reinicios |
| **Stop-check batch 8→2** | 8.06 s/bloque | **7.57 s/bloque** (ratio 1.04×) | **+6.1%** | bit-exact 8/8 en todo el sweep; confirmado en sesión larga de 20 bloques (7.47 vs 8.06) |
| **Pipeline QA/generación** (cliente) | 127.1 s por 10 bloques (QA serial 4.20 s/bloque) | 86.3–93.2 s (QA oculto detrás de la generación) | **−30–33% de wall-clock de sesión** | watchdog e2e: 86.3 s total, 10/10 OK, 0 reintentos, mismos WAV y veredictos |

**Balance combinado:** 15.3 → 7.57 s/bloque (**−50.5%**) + ~35% menos de tiempo de sesión con QA.

## Resultado final end-to-end (2026-08-16) — 2.40×

Mismo guion de 20 bloques con QA (voz real `david.wav`, faster-whisper `small` en CPU),
misma máquina, mismo día, seeds aleatorias (producción), 0 fallidos / 0 cuarentena en
ambas configuraciones:

| Sesión | Wall total | Por bloque |
|---|---|---|
| **Baseline original** (eager + QA serial, flujo pre-pipeline) | 412.4 s | 20.43 s (gen 16.08 + QA 4.35) |
| **Actual** (optimize + batch 2 + pipeline) | **171.7 s** | **8.59 s** (QA oculto) |
| **Mejora** | **−58.4% (2.40×)** | −58.0% |

Descomposición: el server aporta −50.6% (16.08 → 7.94 s/bloque, incl. el primer bloque
frío) y el pipeline oculta los 4.35 s de QA por bloque (−21% del wall baseline).
El mismo script pasó de **6 min 52 s a 2 min 52 s**.

Commits en producción: `688d4ed` (pipeline QA + doc), `3f70472` (batch 2).

## Arranque y operación (patrón recomendado: NO reiniciar)

- **Server → primer WAV:** ~63 s = 40 s de carga (caché inductor caliente; 82 s el primer arranque de la historia) + ~20 s del watchdog (QA 1.2 s + setup 1 s + primer request 14.9 s + QA 4.2 s).
- **El primer bloque cuesta ~2× el estacionario:** 14.9 s (build del cache de voz 4.5 s + inferencia fría 10.4 s) vs 7.57 s estable. Coste único por sesión del server.
- **Cada reinicio evitado ahorra ~48 s** (41 s de carga + ~7 s del primer bloque frío). Mantener el server vivo: 48 s una vez; reiniciar por script: +48 s por script (~240 s/día con 5 scripts).
- **VRAM:** plana en 30+ peticiones seguidas (reserved 6358–6362 MiB, alloc 5443); pico de 7983 MiB (97.5% de 8 GB) en el decode fp32 del VAE → el `empty_cache()` por petición es imprescindible.
- **GPU saturada:** 96–100% SM durante toda la generación y GPU ≈ wall (6.90 s CUDA vs 6.91 s wall) → sin cuello de CPU/lanzamiento. El server está en el límite del hardware.

## Descartados con datos (no especulación)

| Palanca | Resultado | Motivo |
|---|---|---|
| `TORCHINDUCTOR_MAX_AUTOTUNE=1` | **0%** (7.58 vs 7.57 s, 2 pasadas) | kernels heurísticos ya óptimos; +3.5 min de arranque y numerics distintos |
| `VOXCPM_STOP_CHECK_BATCH=16` | **−10.4%** (8.89 s) | overshoot de patches descartados; el 8 tampoco era óptimo (curva: 1–2 mejor) |
| `cudnn.benchmark` | −36% | re-autotunea con cada longitud nueva del VAE (doc anterior) |
| Ventana de atención KV | −12% | slice no contiguo → kernel SDPA peor (doc anterior) |
| TF32 | ~1.5% | solo toca el decode fp32 del VAE; pierde bit-exactness |
| VAE en fp16 | ~1.5% | coste despreciable, riesgo de numerics |
| Recompilaciones por shape | **0** en 30+ peticiones (hasta 200 chars) | único recompile en el warmup del arranque (guard de estructura base/residual LM, de una sola vez) |
| Precarga del cache de voz en el arranque | **lavado (~0 neto)** | el build de 4.5 s está en el camino crítico igualmente; la inferencia fría no se puede precargar |

**Fuera por la restricción de calidad** (son palancas de calidad, no flags): `inference_timesteps` 12→10 (−11% estimado, la DiT/LM escalan con los pasos) y CFG parcial (−16% estimado). Cualquiera de las dos cambiaría la salida (categoría "cambio de seed").

## Reproducibilidad

- Con el entorno actual, `--optimize` es **reproducible entre runs y entre reinicios** (8/8 hashes SHA-256, tiempos ±0.01 s).
- Los baselines optimize de julio (`bench_triton_compile*.json`) **ya no son referencia de hashes**: se reinstalaron torch (14-ago) y triton-windows (12-ago) después, y los kernels compilados cambiaron de numerics. El eager sí sigue siendo 8/8 bit-exact contra su baseline.
- `--optimize` no es bit-exact vs eager (equivale a cambiar de seed: misma calidad en distribución, duraciones a <5%, 0 anomalías).

## Estado final

Todo aplicado y activo en producción sin tocar ningún `.bat`: `iniciar_voxcpm_server.bat` ya usa `--optimize` + batch 2, y `watch_generate.py` hereda el pipeline QA automáticamente. Detalle de cada medición en `docs/optimizacion-inferencia.md`.
