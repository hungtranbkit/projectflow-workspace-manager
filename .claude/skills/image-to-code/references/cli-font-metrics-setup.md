# Preparação de ambientes: CLIs, fontes e métricas

## 1. CLIs de agentes

A execução local prepara os diretórios nativos de skill sem chamar modelos nem usar credenciais:

```bash
python scripts/prepare_cli_environments.py --skill-root . --out artifacts/cli-preflight --check
```

Para instalar CLIs npm em um **ambiente descartável** conectado à rede, use o workflow manual **CLI runtime environment validation** do GitHub Actions. Cada execução aceita **um agente**, evita instalações globais, registra a versão real do binário (quando possível) e informa `NOT_AUTHENTICATED`. O operador revisa o log e o JSON. Instalação e versão não são prova de invocação real da skill.

| Agente | Instalador npm documentado | Comando | Diretório nativo |
|---|---|---|---|
| Claude Code | `@anthropic-ai/claude-code` | `claude` | `.claude/skills/image-to-code` |
| Codex | `@openai/codex` | `codex` | `.agents/skills/image-to-code` |
| OpenCode | `@opencode/cli` | `opencode` | `.opencode/skills/image-to-code` |
| Freebuff | `freebuff` | `freebuff` | `.agents/skills/image-to-code` |
| Codebuff | `codebuff` | `codebuff` | `.agents/skills/image-to-code` |
| Gemini CLI | `@google/gemini-cli` | `gemini` | `.gemini/skills/image-to-code` |
| Hermes | Instalador oficial Hermes, revisão manual | `hermes` | `.hermes/skills/image-to-code` |
| Cursor CLI | Instalador oficial Cursor, revisão manual | `agent` | `.cursor/skills/image-to-code` |

Por segurança, não se executam scripts externos baixados via pipe sem revisão; Hermes e Cursor aparecem como `MANUAL_OFFICIAL_INSTALL_REQUIRED`. Para invocação autônoma imagem → componentes → navegador → correção é necessária uma sessão autenticada com capacidade de visão, conectividade, autorização de ações e benchmark aprovado. Nunca armazenar tokens em logs/artefatos. As credenciais devem ser configuradas no serviço apropriado e só após consentimento.

## 2. Verificação definitiva de fontes

```bash
python -m pip install fonttools
python scripts/verify_font_identity.py --manifest examples/font-source-manifest.example.json --root . --out artifacts/font-identity.json
```

O manifesto deve apontar para **arquivos de fontes licenciados disponibilizados pelo titular**, mantidos fora do GitHub público ou informar hash SHA-256 confirmado pelo proprietário. A aprovação de identidade exige `IDENTICAL_BYTES` ou `IDENTICAL_TO_APPROVED_SHA256`. Sem os arquivos originais ou hash autorizado, o resultado correto é **ORIGINAL_UNAVAILABLE**. A inspeção inclui família, PostScript, peso, largura, itálico e `units_per_em`. Comparações de `font-family` CSS, kerning e contagem de linhas no navegador complementam a checagem, mas isoladamente não provam qual fonte original foi usada. **Nunca publicar font binaries.**

## 3. Histórico de CI

O workflow **Aggregate Image to Code QA history** é acionado quando o CI termina, todas as segundas-feiras e manualmente. Usa somente `GITHUB_TOKEN` e API de Actions com escopo `actions:read`. Publica JSON, CSV, Markdown e HTML como artefatos (`image-to-code-qa-history`). Não modifica a branch principal nem envia imagens/fontes/código-fonte nos artefatos.

Runs históricos sem `qa-metrics-*.zip` ou com artefatos expirados são `NO_ARTIFACT_OR_EXPIRED`, e números são **N/D**, nunca zero. Com os novos artefatos do CI, passam a existir séries por execução e métricas conhecidas. Capturas de outras pessoas e conteúdo privado não podem aparecer no dashboard público.

Comando offline:

```bash
python scripts/ci_metrics.py aggregate --input examples/ci-history-fixture.json --out artifacts/qa-history-example
```

**Os indicadores não são nota de fidelidade de 0 a 10.** Um ciclo aprovado no CI prova que as ferramentas funcionam, não que qualquer screenshot será reproduzido sem intervenção.