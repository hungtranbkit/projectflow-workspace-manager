# Padrão obrigatório de demonstração visual — v1.0.2

Após a implementação, testes e capturas, a entrega visual deve ocorrer **dentro da conversa**, e não apenas como links.

## Ordem da entrega

**Prancha 1 — MOBILE + TABLET (lado a lado)**

- Sempre obrigatória para qualquer reconstrução que produza uma UI responsiva.
- Uma única imagem de duas colunas: MOBILE à esquerda, TABLET à direita; registrar larguras reais de captura.
- Mostrar capturas reais da implementação no mesmo estado funcional sempre que possível.
- Preservar a imagem por inteiro: sem recorte oculto, sem deformação, sem esconder elementos problemáticos. Dimensionar proporcionalmente e usar fundo neutro se sobrar espaço.
- Não incluir um desktop miniaturizado nessa prancha; não fazer um mosaico com dezenas de assets.

**Prancha 2 — DESKTOP**

- Quando o arquivo enviado for um **print de interface desktop**, sempre exibir **REFERÊNCIA ORIGINAL | RECONSTRUÇÃO DESKTOP**, lado a lado e com rótulos claros.
- Capturar a reconstrução no viewport correspondente à área original da interface (mesmo tamanho, zoom, estado e DPR sempre que determinável). Se houver barras de sistema/browser fora da UI, só cortar explicitamente com os limites registrados.
- Caso as dimensões não correspondam, a prancha é ilustrativa, deve exibir aviso visível e **não serve como comprovação de fidelidade**. Recapturar com as mesmas dimensões antes da auditoria.
- Quando a referência for mobile ou tablet, mostrar uma captura desktop individual, sem fabricar uma falsa referência desktop.

## Comando

```bash
python scripts/present_results.py \
  --mobile artifacts/screenshots/mobile.png \
  --tablet artifacts/screenshots/tablet.png \
  --desktop artifacts/screenshots/desktop.png \
  --reference input/screenshot-desktop.png \
  --desktop-reference-render artifacts/screenshots/desktop-same-viewport.png \
  --reference-kind desktop \
  --out artifacts/presentation
```

Se o print original contiver barras que não pertencem à interface, informar o recorte EXPLÍCITO:

```bash
  --reference-crop 0 16 1179 616
```

Para uma referência mobile, use `--reference-kind mobile`, ou `auto` com confirmação humana do tipo detectado. Não forneça um argumento de referência falso; sem referência, a segunda prancha é uma captura desktop sem comparação. A detecção `auto` considera a proporção da imagem e pode estar errada para prints complexos.

## Saídas

- `artifacts/presentation/mobile-tablet-side-by-side.png`
- `artifacts/presentation/desktop-original-vs-render.png` **ou** `desktop-preview.png`
- `artifacts/presentation/presentation-report.json` com dimensões, escala de apresentação, recorte explícito e avisos.

## Contrato da resposta

O agente deve exibir **as duas imagens inline no chat** na sequência descrita, antes de links para arquivos. Exemplo, em Markdown quando suportado:

```markdown
### Mobile e tablet
![Mobile e tablet lado a lado](artifacts/presentation/mobile-tablet-side-by-side.png)

### Desktop — original × reconstrução
![Comparação desktop](artifacts/presentation/desktop-original-vs-render.png)
```

Adapte a referência de imagem ao meio onde a conversa é exibida; não assuma que um caminho relativo local é acessível ao usuário. Arquivos sem URL verificável podem exigir apresentação via ferramenta de imagens. Se imagens inline não forem suportadas, informar essa limitação objetivamente e anexar os arquivos, sem fingir que apareceram no chat.

**Nunca:** mostrar um único mosaico gigante de original + mobile + tablet + desktop + assets; usar apenas links; cortar partes da página sem informar; chamar adaptação inferida de comparação pixel-perfect.
