# Roda o comparador de promoções todo dia e publica o relatório no GitHub Pages.
# Caminho no repositório:  .github/workflows/promocoes.yml
name: Promoções da semana

on:
  schedule:
    - cron: "43 11 * * *"   # todo dia ~6h43 em Québec (horário UTC no GitHub)
    - cron: "43 22 * * 3"   # quarta à noite, para pegar os folhetos da semana seguinte
  workflow_dispatch:         # botão "Run workflow" para rodar na hora
  push:                      # roda sozinho quando você atualiza algum destes arquivos
    branches: [main]
    paths: ["promocoes.py", "modelo_app.html", "lista_compras.txt", ".github/workflows/promocoes.yml"]

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  gerar:
    runs-on: ubuntu-latest
    env:
      TZ: America/Toronto    # datas e horários no fuso de Québec
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - name: Buscar promoções e gerar relatório
        run: |
          mkdir -p site
          python promocoes.py --nao-abrir --sem-cache --csv --saida site/index.html
      - uses: actions/configure-pages@v6
      - uses: actions/upload-pages-artifact@v5
        with:
          path: site

  publicar:
    needs: gerar
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - id: deploy
        uses: actions/deploy-pages@v5
