# 🚀 Disparador WhatsApp PRO

Este é um sistema completo para disparo de mensagens em massa via WhatsApp utilizando a **Evolution API** e o framework **FastAPI** (Python).

## 📂 Estrutura do Projeto

- **`backend/`**
  - `main.py`: Coração do sistema. Contém as rotas da API (FastAPI), integração com banco de dados e comunicação com a Evolution API.
  - `models.py`: Modelos de tabela do banco de dados (SQLAlchemy).
  - `schemas.py`: Validação de dados de entrada/saída (Pydantic).
  - `db.py`: Conexão com o banco de dados (PostgreSQL/SQLite).
- **`frontend/`**
  - `index.html`: Interface visual (Dashboard) construída com TailwindCSS. Sem dependências pesadas, consome a API diretamente via Fetch.
- **`dados/`**
  - `leads.csv`: Planilha principal com os contatos e status. Ela é atualizada em tempo real após cada disparo para manter sincronia com o banco.

## ⚙️ Como o Fluxo Funciona

1. **Conexão**: O frontend consome a rota `/whatsapp/connect` para buscar o QR Code da Evolution API.
2. **Gerenciador de Mensagens**: O usuário cadastra múltiplas variações de mensagens através da interface. O sistema as armazena no banco de dados para uso nos disparos.
3. **Importação**: Os leads são lidos de um arquivo `.csv` (separado por `,` ou `;`) pelo endpoint `/upload-leads-file` ou puxados automaticamente de `/dados/leads.csv` pelo endpoint `/import-local-leads`.
4. **Inteligência de Importação**: O sistema identifica o número, remove caracteres especiais e adiciona o código do Brasil (`55`). Contatos já existentes têm seu `status` atualizado para ficar em sincronia com a planilha.
5. **Disparo Inteligente**: O endpoint `/send` executa uma lógica avançada para cada lead pendente:
   - **Sorteia uma mensagem aleatória** da lista de templates cadastrados.
   - Se uma imagem for anexada, **sorteia a ordem de envio** (imagem antes do texto ou texto antes da imagem).
   - **Aplica um delay aleatório** (2 a 8 segundos) entre o envio do texto e da imagem (se houver).
   - **Aplica um delay longo e aleatório** (20 a 90 segundos) entre o envio para um lead e o próximo, simulando comportamento humano para evitar bloqueios.
6. **Sincronização**: Ao final do disparo, o banco de dados é atualizado e o arquivo físico (`dados/leads.csv`) é reescrito com o status final de cada lead (`enviado`, `falhou` ou `pendente`).

## 🗄️ Configuração do Banco de Dados (PostgreSQL)

O sistema utiliza o **PostgreSQL** para armazenar os leads de forma segura e evitar disparos duplicados.

1. Certifique-se de ter o PostgreSQL rodando na sua máquina (na porta padrão `5432`).
2. Crie um banco de dados chamado `disparador`.
3. A conexão está configurada no arquivo `backend/db.py` usando a credencial padrão:
   `postgresql://postgres:admin@localhost:5432/disparador` *(usuário: postgres, senha: admin)*. Caso as credenciais do seu servidor sejam diferentes, altere a URL neste arquivo.

## 📱 Configuração da Evolution API

A comunicação com o WhatsApp depende da Evolution API. As variáveis que garantem essa conexão estão fixadas no topo do arquivo `backend/main.py`:

```python
EVOLUTION_URL = "http://127.0.0.1:8080"
API_KEY = "ev_api_123456_mt_local"
INSTANCE = "minha_instancia"
```
*⚠️ Atenção: Caso você hospede a Evolution API em um servidor externo (VPS) ou crie uma instância com um nome diferente, lembre-se de atualizar esses valores no código!*

## 🎨 Frontend (Interface)

A interface do usuário é um arquivo HTML único e autossuficiente (`frontend/index.html`) que não requer um servidor web ou processo de build para rodar.

- **Estilo**: O layout e o design são construídos com **Tailwind CSS**, carregado via CDN. Isso permite um design moderno e responsivo sem a necessidade de arquivos CSS separados.
- **Ícones**: Os ícones são fornecidos pela biblioteca **Font Awesome**, também carregada via CDN.
- **Lógica**: Todo o código JavaScript para interagir com o backend (buscar QR Code, carregar leads, fazer upload, disparar mensagens) está contido em uma tag `<script>` dentro do próprio `index.html`, utilizando a API `fetch` nativa do navegador.

## 🚀 Como Rodar o Projeto

**1. Iniciar a Evolution API (Terminal Node.js):**
```powershell
cd C:\evolution
npm run dev:server
```

**2. Iniciar o Backend FastAPI (Terminal Python):**
```powershell
cd C:\Users\Acer\Downloads\Disparador2\backend
C:/Users/Acer/AppData/Local/Python/pythoncore-3.14-64/python.exe -m uvicorn main:app --reload
```

**3. Abrir o Frontend:**
Basta ir até a pasta `frontend/` e dar dois cliques no arquivo `index.html` para abri-lo no seu navegador favorito.

## 💡 Dicas e Cuidados
- Para máxima eficácia e segurança, cadastre pelo menos 5-10 variações de mensagens no "Gerenciador de Mensagens".
- Se a planilha vier do Excel com formatação estranha, o backend possui uma "rede de segurança" que limpa quebras de linha defeituosas (`\r\n`) e bytes nulos (`\x00`).
- **Nunca** realize um disparo se o arquivo `leads.csv` estiver aberto em uso no Excel, pois o Windows pode bloquear o Python de escrever o status atualizado de volta no arquivo.

## 💾 Controle de Versão (Git)

Este projeto é versionado com Git e hospedado em um repositório privado no GitHub para garantir a segurança e o histórico das alterações.