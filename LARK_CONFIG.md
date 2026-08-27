# Configuração das 2 Bases no Lark

Este projeto **não usa API do Lark**. Os dados saem das automações da Base por **Solicitação de HTTP (POST)**.

## Antes de configurar o Lark

Depois do deploy no Render, copie:

1. A URL pública do serviço, por exemplo: `https://painel-lark-producao.onrender.com`
2. O valor da variável `PAINEL_KEY` em **Render > Environment**.

Em todas as solicitações HTTP do Lark use o header:

```text
Content-Type: application/json
X-Painel-Key: COLE_AQUI_O_VALOR_DE_PAINEL_KEY
```

---

## BASE 1 — OEE / Produção

### URL

```text
POST https://SEU-SERVICO.onrender.com/lark/oee
```

### Recomendado

Na automação:

1. Gatilho: registro criado/modificado, ou agendamento.
2. Ação: **Procurar registros** do mês atual.
3. Ação: **Solicitação de HTTP**.
4. Envie o conjunto encontrado dentro da propriedade `registros`.

O backend aceita tanto **um registro** quanto **um pacote de registros**. Quando recebe uma lista, ele substitui a fotografia anterior da Base 1, o que evita manter registros que foram apagados no Lark.

### Campos reconhecidos

- `Data`, `Fórmula` ou `data`
- `Turno`
- `Plano Diário`
- `Produção Real`
- `Faltas`
- `Efetivo`
- `DOWNTIME`
- `Dias Restantes`
- `OEE`
- `Absenteísmo %`
- `Plano Mensal`
- `Linha`
- `Unidade`

### Exemplo de teste manual

```json
{
  "planoMensal": 51480,
  "linha": "LINHA 04",
  "unidade": "CONDENSADORA",
  "registros": [
    {
      "Data": "27/08/2026",
      "Turno": "1º TURNO",
      "Plano Diário": 1247,
      "Produção Real": 1180,
      "Faltas": 3,
      "Efetivo": 73,
      "DOWNTIME": "00:12"
    },
    {
      "Data": "27/08/2026",
      "Turno": "2º TURNO",
      "Plano Diário": 1164,
      "Produção Real": 640,
      "Faltas": 1,
      "Efetivo": 73,
      "DOWNTIME": "00:45"
    }
  ]
}
```

> No Lark, não digite `{{campo}}` manualmente se a interface permitir escolher valores dinâmicos. Insira os campos pelo seletor da automação.

---

## BASE 2 — Programação / Lotes

### URL

```text
POST https://SEU-SERVICO.onrender.com/lark/programacao
```

### Recomendado

1. Gatilho: registro criado/modificado, ou agendamento.
2. Ação: **Procurar registros** relevantes para a linha.
3. Ação: **Solicitação de HTTP**.
4. Envie a lista dentro de `registros`.

### Campos reconhecidos

- `LINHA DE PRODUÇÃO`
- `LOTE`
- `ORDEM DE PRODUÇÃO`
- `CÓDIGO DO PRODUTO`
- `DESCRIÇÃO DO MODELO`
- `SÉRIE DO PRODUTO`
- `UNIDADE`
- `QTD` ou `quantidade`
- `QTD. TOTAL PRODUZIDA`
- `FALTA PRODUZIR`
- `STATUS DE PRODUÇÃO`
- `PREV. INÍCIO`
- `PREV. TÉRMINO`
- `CAP/HR`
- `OBS`

### Exemplo de teste manual

```json
{
  "registros": [
    {
      "LINHA DE PRODUÇÃO": "LINHA 04",
      "LOTE": "W7040",
      "ORDEM DE PRODUÇÃO": "Z02006748",
      "DESCRIÇÃO DO MODELO": "GWC12ATB-D6DNA2BB/O",
      "SÉRIE DO PRODUTO": "G-CLASSIC INVERTER 2026 C",
      "QTD": 3650,
      "QTD. TOTAL PRODUZIDA": 756,
      "FALTA PRODUZIR": 2894,
      "STATUS DE PRODUÇÃO": "PRODUZINDO"
    }
  ]
}
```

### Exclusões

O método mais confiável é sempre enviar a lista completa retornada por **Procurar registros**. Assim, quando um registro for apagado no Lark, ele desaparece na próxima sincronização.

Se preferir mandar registro a registro, o backend também entende:

```json
{
  "acao": "excluir",
  "ORDEM DE PRODUÇÃO": "Z02006748"
}
```

---

## Testes depois do deploy

Abra no navegador:

```text
https://SEU-SERVICO.onrender.com/health
```

Deve aparecer `ok: true`.

Depois:

```text
https://SEU-SERVICO.onrender.com/api/status
```

Mostra quantos turnos e lotes estão salvos.

E:

```text
https://SEU-SERVICO.onrender.com/api/dashboard
```

Mostra o JSON que o dashboard está lendo.
