# MEDICUS PRIME — Medical Watch

Motor de vigilancia científica médica para alimentar el conocimiento de un Gem/Skill de Gemini.

## Arquitectura

PubMed + Europe PMC -> selección por recencia y tipo de estudio -> curación opcional con Gemini -> knowledge/medical_update.md y knowledge/medical_update.json.

GitHub Actions ejecuta el motor diariamente.

## Fuentes

- PubMed / NCBI E-utilities
- Europe PMC
- Gemini API para curación científica opcional

## Configuración

Crea el secreto `GEMINI_API_KEY` en Settings -> Secrets and variables -> Actions.

Puedes definir `GEMINI_MODEL` como variable del repositorio. El valor recomendado actualmente es `gemini-3.8-flash`.

## Ejecución

```bash
pip install -r requirements.txt
python src/medical_watch.py
```

El sistema genera:

- `knowledge/medical_update.md`: conocimiento legible para Gemini.
- `knowledge/medical_update.json`: datos estructurados.
- `state/rotation.json`: estado de rotación temática.

## Seguridad científica

El sistema no presenta hallazgos preliminares como práctica clínica establecida y no inventa referencias, DOI, PMID, resultados ni conclusiones.
