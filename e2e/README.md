# E2E Tests — Cycle Configurable v2

6 pruebas manuales en 2 rondas para verificar que el ciclo configurable funciona end-to-end con un LLM real.

- **Ronda 1** (01-03): Validación de los 3 patrones base (one-shot, research, dev)
- **Ronda 2** (04-06): Stress tests — validation loops, multi-doc research, dev incremental

## Setup

Cada test usa `{CWD}` como placeholder. Antes de ejecutar, reemplázalo con un directorio temporal real:

```bash
# Crear workspace temporal por test
mkdir -p /tmp/tao-e2e/01 /tmp/tao-e2e/02 /tmp/tao-e2e/03

# Reemplazar placeholder (o editar los JSON manualmente)
sed -i 's|{CWD}|/tmp/tao-e2e/01|' e2e/01-one-shot.json
sed -i 's|{CWD}|/tmp/tao-e2e/02|' e2e/02-research-linear.json
sed -i 's|{CWD}|/tmp/tao-e2e/03|' e2e/03-dev-with-validation.json
```

## Test 1: One-shot (sin scope, 1 paso LLM)

**Qué prueba**: El caso más simple — ciclo con un solo step, sin scope.

```bash
tao run e2e/01-one-shot.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] Existe `/tmp/tao-e2e/01/hello.py`
- [ ] `python /tmp/tao-e2e/01/hello.py` imprime "Hello from TAO cycle v2!"
- [ ] Traces: 1 trace con role `implement`

```bash
tao status          # → completed
tao traces <id>     # → 1 trace: implement
python /tmp/tao-e2e/01/hello.py
```

## Test 2: Research linear (scope + gather + write)

**Qué prueba**: Scope descompone en subtasks, ciclo lineal de 2 pasos LLM sin loops.

```bash
tao run e2e/02-research-linear.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] Scope produce ≥1 subtask
- [ ] Existe `/tmp/tao-e2e/02/dataclasses-cheatsheet.md`
- [ ] El fichero tiene contenido real sobre dataclasses
- [ ] Traces: scope + (gather + write) × N subtasks + re-scope(s)

```bash
tao status          # → completed
tao traces <id>     # → scope, gather, write, scope (re-scope)
cat /tmp/tao-e2e/02/dataclasses-cheatsheet.md
```

## Test 3: Dev con validation loop (plan + implement + validate + fix)

**Qué prueba**: El patrón dev completo con command step y on_fail jump.

```bash
tao run e2e/03-dev-with-validation.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] Existen `/tmp/tao-e2e/03/fizzbuzz.py` y `/tmp/tao-e2e/03/test_fizzbuzz.py`
- [ ] `python -m pytest /tmp/tao-e2e/03/test_fizzbuzz.py -v` pasa
- [ ] Traces: plan + implement + validate (+ fix + validate si hubo errores)
- [ ] Si validate pasó a la primera: NO hay traces de `fix`
- [ ] Si validate falló: hay traces de `fix` → `validate` (loop)

```bash
tao status          # → completed
tao traces <id>     # → plan, implement, validate [, fix, validate]
python -m pytest /tmp/tao-e2e/03/test_fizzbuzz.py -v
```

## Qué cubre cada test (Ronda 1)

| Test | Scope | Cycle lineal | Command step | on_fail jump | max_retries |
|------|-------|-------------|--------------|-------------|-------------|
| 01   | —     | ✓ (1 paso)  | —            | —           | —           |
| 02   | ✓     | ✓ (2 pasos) | —            | —           | —           |
| 03   | —     | ✓ (4 pasos) | ✓            | ✓           | ✓           |

## Limpieza (Ronda 1)

```bash
rm -rf /tmp/tao-e2e
```

---

## Ronda 2 — Tests más gordos

Setup:

```bash
mkdir -p /tmp/tao-e2e-r2/04 /tmp/tao-e2e-r2/05 /tmp/tao-e2e-r2/06

sed -i 's|{CWD}|/tmp/tao-e2e-r2/04|' e2e/04-one-shot-with-validation.json
sed -i 's|{CWD}|/tmp/tao-e2e-r2/05|' e2e/05-research-multi-doc.json
sed -i 's|{CWD}|/tmp/tao-e2e-r2/06|' e2e/06-dev-incremental.json
```

## Test 4: One-shot con validation loop

**Qué prueba**: Ciclo completo (plan → implement → validate → fix) sin scope. Ejerce el intérprete de ciclo con command steps y jumps en modo one-shot. Edge cases de temperatura dan probabilidad real de fix loop.

```bash
tao run e2e/04-one-shot-with-validation.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] Existen `/tmp/tao-e2e-r2/04/converter.py` y `/tmp/tao-e2e-r2/04/test_converter.py`
- [ ] `python -m pytest /tmp/tao-e2e-r2/04/test_converter.py -v` pasa
- [ ] Traces: plan → implement → validate (→ fix → validate si hubo errores)
- [ ] Mínimo 2 traces LLM (plan + implement), máximo 4+ si hubo fix loop

```bash
tao status
tao traces <id>
python -m pytest /tmp/tao-e2e-r2/04/test_converter.py -v
```

## Test 5: Research multi-doc con síntesis

**Qué prueba**: Scope produce múltiples subtasks → cada una genera un doc independiente → doc de síntesis final. Ejerce scope con batch_size, re-scope, y múltiples ficheros de output.

```bash
tao run e2e/05-research-multi-doc.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] 4 ficheros .md existen: `threading-analysis.md`, `multiprocessing-analysis.md`, `asyncio-analysis.md`, `concurrency-comparison.md`
- [ ] Cada fichero tiene contenido real (40-80 líneas)
- [ ] Traces: scope + (gather + write) × N + re-scope(s)
- [ ] Al menos 4 subtasks procesadas

```bash
tao status
tao traces <id>
wc -l /tmp/tao-e2e-r2/05/*.md
```

## Test 6: Dev incremental — 3 subtasks sobre el mismo código

**Qué prueba**: Scope descompone en 3 subtasks ordenadas. Cada una modifica/extiende el mismo fichero. Ejerce scope con dependencias implícitas, acumulación de código, y validation sobre código que crece.

```bash
tao run e2e/06-dev-incremental.json
```

**Verificación**:
- [ ] Task completa con status `completed`
- [ ] `task_queue.py` tiene clase TaskQueue con los 3 niveles de funcionalidad
- [ ] `test_task_queue.py` tiene tests para los 3 niveles
- [ ] `python -m pytest /tmp/tao-e2e-r2/06/test_task_queue.py -v` → all pass
- [ ] Traces: scope + 3× (plan + implement + validate [+ fix]) + re-scope(empty)
- [ ] Mínimo 3 subtasks ejecutadas con traces de plan + implement + validate cada una

```bash
tao status
tao traces <id>
python -m pytest /tmp/tao-e2e-r2/06/test_task_queue.py -v
```

## Cobertura comparada R1 vs R2

| Dimensión | R1 | R2 |
|-----------|----|----|
| One-shot complejidad | 1 paso, hello.py | 4 pasos, validation loop, edge cases |
| Research subtasks | 1 doc | 4 docs (3 + síntesis) |
| Research re-scope | trivial | necesario si scope no mete las 4 en un batch |
| Dev subtasks | 1 (FizzBuzz) | 3 (incremental, mismo código) |
| Dev acumulación | — | cada fase modifica código de la anterior |
| Validation real | pytest trivial | pytest con edge cases, probabilidad real de fix loop |
| Coste estimado | ~$0.23 total | ~$2-4 total |

## Ejecución completa (R2)

```bash
tao run e2e/04-one-shot-with-validation.json e2e/05-research-multi-doc.json e2e/06-dev-incremental.json
```

## Limpieza (Ronda 2)

```bash
rm -rf /tmp/tao-e2e-r2
```
