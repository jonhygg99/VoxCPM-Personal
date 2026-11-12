---
name: benchmark-perfil
description: "Barre los ajustes de rendimiento de ESTE ordenador (optimize, VOXCPM_STOP_CHECK_BATCH, modelo de QA), los mide con el benchmark del repo y guarda el ganador en benchmarks/perfil-maquina.json, del que leen sus defaults los scripts del proyecto. Usar al cambiar de GPU/driver/torch, cuando el usuario pregunte que ajustes rinden mejor en esta maquina, o antes de buscar cuellos de botella nuevos."
---

# /benchmark-perfil

Mide este PC y deja por escrito los ajustes con los que rinde mejor. No mide
calidad de audio: solo knobs de rendimiento que existen ya en el repo
(`--optimize`, `VOXCPM_STOP_CHECK_BATCH`, modelo/device de faster-whisper) y
nunca toca timesteps/CFG.

## Cuando usar

- El usuario pregunta que ajustes van mejor en este ordenador.
- Cambio la GPU, el driver, la version de torch o se instalo/quito `triton-windows`.
- Se abre un PC nuevo (mismo repo, otra maquina).

## El perfil

- Fichero local: `benchmarks/perfil-maquina.json` (gitignored, ver `benchmarks/.gitignore`).
- Plantilla: `benchmarks/perfil-maquina.ejemplo.json`.
- Lo leen `perfil_maquina.py` (loader con defaults de la RTX 3070 si falla) y con el
  sus defaults `voxcpm_server.py` (`--optimize`, stop-check batch),
  `watch_generate.py` / `generate_script.py` / `generate_shorts.py` (modelo y
  device de QA) y `benchmark_voxcpm_inference.py` (bloques y warmup).

## Comandos

```
/benchmark-perfil                 # barrido completo y guarda el perfil de esta maquina
/benchmark-perfil mostrar          # imprime el perfil vigente (o los defaults si no hay)
/benchmark-perfil barrir           # fuerza re-medir ignorando el perfil actual
```

## Procedimiento

1. **Comprobar el perfil vigente** — leer `benchmarks/perfil-maquina.json`. Si
   existe, su `origen` es `"barrido"` y su `maquina` coincide con la actual
   (misma GPU, torch y triton), usarlo y parar: no hace falta re-medir. Si falta
   o su `origen` es `"defaults-repo-sin-barrer"`, seguir al paso 2.
2. **Requisitos antes de medir** — GPU libre. Si `check_server` responde, hay un
   server levantado y falsearia el barrido: decirlo y parar.
3. **Barrido de servidor** — `python scripts/benchmark_perfil.py` (o
   `--solo-servidor` para saltar el QA, `--dry-run` para ver las combinaciones).
   Por cada combinacion (`optimize` off, y on con batch 1/2/4) levanta un server
   fresco, mide `benchmarks/bench_script.txt` (8 bloques + 2 warmup, seed 1234)
   y guarda `median_ratio`, `avg_inference_seconds` y `peak_reserved_mb`.
4. **Barrido de QA** — carga `small`/`base`/`medium` de faster-whisper en CPU y
   los cronometra sobre `voices/es/david.wav`; gana el mas rapido que SIGUE pasando
   sus propios umbrales (`AudioQA.validate`, ver `audio_qa.py`). Un modelo mas
   rapido que no pasaria el QA se descarta.
5. **Guardar** — escribe `benchmarks/perfil-maquina.json` con `maquina`
   (fingerprint), `servidor`, `qa`, `benchmark` y `metricas` de todas las
   combinaciones como evidencia. Los scripts del proyecto ya lo leen solos.
6. **Verificar** — `python -m pytest tests/test_perfil_maquina.py -q` (valida el
   loader, el ejemplo y la eleccion de ganador) y, si se toco algo mas,
   `python -m pytest tests/ -q`.

## Reglas

- El ganador se elige por `median_ratio` (inferencia/audio, la metrica de
  `docs/optimizacion-inferencia.md`); empate -> menos VRAM reservada.
- Un knob solo entra si puede medirse en esta maquina; nunca copiar el numero de
  otra GPU sin medir.
- Si una combinacion falla o da 0 bloques OK, se ignora (no envenena el perfil).
- No tocar calidad (timesteps, CFG, normalize): eso cambia el audio.
