# Dashboard de Produção — Lark + Render + GitHub

Projeto pronto para receber **duas Bases do Lark sem usar API do Lark**.

A integração é:

```text
Lark Base 1 (OEE) --------POST------> Render /lark/oee
                                          |
                                          +--> /api/dashboard --> dashboard.html
                                          |
Lark Base 2 (Programação)-POST------> Render /lark/programacao
```

## Arquivos

- `app.py` — backend Flask que recebe e combina as duas Bases.
- `dashboard.html` — dashboard para TV, já apontando para `/api/dashboard`.
- `requirements.txt` — dependências Python.
- `render.yaml` — configuração automática do Render.
- `LARK_CONFIG.md` — passo a passo da Solicitação de HTTP nas duas Bases.

## 1. Subir para o GitHub

Crie um repositório e envie **todos os arquivos deste pacote para a raiz**.

Exemplo:

```text
seu-repositorio/
├── app.py
├── dashboard.html
├── LARK_CONFIG.md
├── README.md
├── render.yaml
├── requirements.txt
└── .gitignore
```

## 2. Criar no Render

Você pode usar **New > Blueprint** e apontar para o repositório, porque já existe `render.yaml`.

Ou criar manualmente um Web Service:

```text
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: gunicorn --workers 1 --threads 4 --timeout 120 app:app
```

> O projeto usa **1 worker** de propósito. Os dados recebidos do Lark ficam no estado do processo e em um snapshot local. Com vários workers, cada processo poderia ter uma fotografia diferente.

## 3. Copiar a chave de segurança

O `render.yaml` cria automaticamente a variável:

```text
PAINEL_KEY
```

No Render, abra **Environment** e copie o valor. Use esse valor no Lark como:

```text
X-Painel-Key: SEU_VALOR
```

## 4. Configurar as Bases do Lark

Veja `LARK_CONFIG.md`.

As URLs serão:

```text
POST https://SEU-SERVICO.onrender.com/lark/oee
POST https://SEU-SERVICO.onrender.com/lark/programacao
```

## 5. Abrir o painel

A URL principal do Render já abre o dashboard:

```text
https://SEU-SERVICO.onrender.com/
```

O HTML consulta automaticamente:

```text
/api/dashboard
```

A cada 30 segundos.

## 6. Diagnóstico

```text
GET /health
GET /api/status
GET /api/dashboard
```

### Exemplo `/api/status`

```json
{
  "ok": true,
  "turnos": 20,
  "lotes": 14,
  "linha": "LINHA 04",
  "planoMensal": 51480,
  "protegido": true
}
```

## Persistência no Render

O projeto salva um snapshot local para tolerar reinícios do processo, porém no Render sem Persistent Disk o filesystem é **efêmero**: um novo deploy ou troca de instância pode apagar esse snapshot.

Para este fluxo, a configuração recomendada é fazer a automação do Lark enviar **a lista completa** obtida por `Procurar registros`, e opcionalmente criar também uma automação agendada para reenviar periodicamente. Assim o Render consegue reconstruir o painel sem usar API do Lark nem banco externo.

Se futuramente você contratar Persistent Disk no Render, configure `DATA_DIR` para o ponto de montagem do disco.
