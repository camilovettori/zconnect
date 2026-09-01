# Prompts para Claude Code (VS Code) — Correções zconnect UTZ

Copiar e colar cada prompt diretamente no Claude Code dentro do VS Code, com o projeto UTZ aberto.

---

## Correção 1 — VAT errado (23% em vez de 13.5%)

```
Os seguintes produtos estão sendo importados com Standard Tax Rate (23%) quando deveriam usar Reduced Tax Rate (13.5%):

- Biscoff Blondie
- Biscoff Blondies Half
- Biscoff Box of 4
- Cinnamon Rolls
- Cream Cheese Frosting on a Piping Bag (2 produtos)
- Chocolate Coconut Almond (este deve ficar em 23%)

O problema está na função `resolve_tax_id_for_product()` em `backend/app/services/export_service.py` (linhas 60-84). Quando o Unify não envia `tax_percentage` no item, a função cai no fallback da linha 84 que retorna `ZOHO_STANDARD_TAX_ID` (23%).

Preciso que:
1. Adicione os nomes desses produtos à lista `BAKERY_TAX_KEYWORDS` (linhas ~30-50) para que sejam reconhecidos como 13.5% pelo name matching. Use keywords genéricas o suficiente para apanhar variações (ex: "biscoff", "cinnamon roll", "cream cheese frosting").
2. Confirme que "chocolate coconut almond" NÃO está nas bakery keywords (deve continuar 23%).
3. Verifique se a lógica de `_extract_tax_percentage()` em `backend/app/services/unify_service.py` (linha 1719) está realmente tentando todos os campos possíveis que o Unify pode enviar.

Não altere a lógica de fallback geral — apenas adicione os keywords certos para estes produtos específicos.
```

---

## Correção 2 — Desincronizar e sincronizar invoice por unidade (não por grupo do dia)

```
O cliente precisa poder desincronizar (unsync) uma invoice individual e sincronizar uma por uma, em vez de trabalhar com o batch do dia inteiro.

Atualmente:
- O backend tem `POST /export/reset-sync-state` (routes.py linha 876) que reseta TUDO
- Tem `POST /export/reset-selected-runs` (routes.py linha 950) que reseta por sync_run_id
- Mas NÃO tem um endpoint para resetar um order_id individual

Preciso que:
1. Crie um novo endpoint `POST /export/reset-order/{order_id}` em `backend/app/api/routes.py` que:
   - Encontre o `ExportedOrder` pelo `unify_order_id` (model em `backend/app/models.py` linha 36)
   - Delete apenas esse registro do `exported_orders`
   - Não toque nos outros orders do mesmo sync_run
   - Retorne sucesso com o order_id resetado

2. No frontend `frontend/app/sync/page.tsx`:
   - O botão "Sync this order" (linha 1473) já existe e funciona para sincronizar individual — está OK
   - Adicione um botão "Unsync" ao lado do "Sync this order" que só aparece quando `order.already_exported === true` ou `getOrderStatus(order) === "already_synced"`
   - Esse botão deve chamar o novo endpoint e depois atualizar o estado local para marcar o order como não sincronizado

O objetivo é que o operador possa desfazer o sync de uma order da camerino sem afetar as outras.
```

---

## Correção 3 — Bodega Howth criando customer duplicado

```
O customer "Bodega Howth" não está sendo encontrado no Zoho quando uma invoice é sincronizada. O sistema cria um novo customer toda vez, gerando duplicatas.

O fluxo de resolução de customer está em `backend/app/services/export_service.py`, método `_resolve_contact_for_order()` (linha 747). Ele usa o `_buyer_key()` (linha 478) que retorna `order.buyer_id or order.order_id`.

O problema provável é um de dois cenários:
a) O `buyer_id` do Unify muda entre orders, então o `buyer_key` é diferente a cada vez e o `CustomerMapping` nunca encontra o cache
b) O nome no Zoho é ligeiramente diferente do nome que o `find_contact_by_name()` (zoho_service.py linha 159) procura — a busca é por nome exato (`contact_name` param)

Investiga:
1. Verifica o método `find_contact_by_name()` em `backend/app/services/zoho_service.py` (linha 159) — ele faz match exato. Adiciona uma busca fuzzy/parcial: se o match exato falhar, tenta `contact_name_contains` ou normaliza ambos os nomes (lowercase, strip) antes de comparar.
2. No `_resolve_contact_for_order()`, depois de falhar o lookup por `buyer_key` no `CustomerMapping`, adiciona um fallback que tenta procurar pelo `customer_name` normalizado também (não só pelo `buyer_key`).
3. Adiciona logging extra para mostrar exatamente qual `buyer_key` e `lookup_query` estão sendo usados para Bodega Howth, para ajudar a diagnosticar.
4. Considera adicionar um endpoint ou UI para manual customer mapping — onde o operador pode manualmente associar um nome Unify a um contact_id Zoho existente.
```

---

## Correção 4 — Delivery fee não está sendo puxado

```
O Unify mudou a estrutura do delivery fee. Antes era um produto na lista de items; agora é uma linha separada na secção "Taxes & Charges", após o total dos produtos. O zconnect não está a capturar este valor.

O parsing do delivery fee está em `backend/app/services/unify_service.py`:
- Linhas 1955-1975: tenta extrair `totalDeliveryFee` / `delivery_fee` do `detail` object
- Linha 1961: verifica se já existe um delivery item nas linhas
- Se encontrar o valor e não houver delivery item, cria uma OrderLine com SKU "DELIVERY"

O mesmo padrão repete-se nas linhas 3085-3103 (segunda função de hydration).

O problema é que o Unify agora coloca o delivery fee numa estrutura diferente. Preciso que:

1. Adicione logging detalhado no início da hydration para imprimir as keys do `detail` object — preciso ver exatamente onde o Unify está a colocar o delivery fee agora.
2. Inspeccione o campo `detail` à procura de novas estruturas possíveis:
   - `detail.charges` ou `detail.taxesAndCharges` como array com entries tipo `{"name": "Delivery", "amount": 10.00}`
   - `detail.additionalCharges` 
   - nested dentro de um segundo bloco de taxes
3. Atualize `_to_major_currency()` call para tentar esses novos campos
4. Faça a mesma correção nas DUAS funções de hydration (linhas ~1955 e ~3085) — o código está duplicado

Se não conseguires determinar a estrutura exata, adiciona pelo menos logging de debug em `UNIFY_DEBUG_SHAPES=true` que imprima o `detail` completo quando o delivery fee é 0 mas o order tem entrega.
```

---

## Correção 5 — Remover delivery note das invoices

```
O cliente quer remover o seguinte texto que aparece em todas as invoices como nota:

"This is your delivery receipt and invoice. A weekly statement will be e-mailed to you. Thank you for loving our cake! Caryna and the Lovin' from the Oven team"

Este texto está hardcoded em `backend/app/services/export_service.py`, no método `build_invoice_notes()` (linhas 539-562).

Preciso que:
1. Remove as linhas 556-559 que adicionam essa mensagem fixa
2. Mantém a linha do VAT number (linha 561): "VAT IE9802711J"
3. Mantém a lógica de storage notes (linhas 540-553) — isso é útil
4. O resultado final do `build_invoice_notes()` deve incluir apenas:
   - Storage note (se existir)
   - "VAT IE9802711J"
   - Nada mais

O campo `notes` é passado no payload da invoice em `_build_draft_invoice_payload()` na linha 902.
```

---

## Correção 6 — Ticar GoCardless, Stripe e emails automaticamente

```
Ao criar uma invoice no Zoho, o cliente quer que automaticamente:
- GoCardless esteja marcado como payment gateway
- Stripe esteja marcado como payment gateway
- Todos os emails de contato do customer estejam selecionados para comunicação

Atualmente o payload da invoice é construído em `backend/app/services/export_service.py`, método `_build_draft_invoice_payload()` (linha 896). O payload enviado ao Zoho (via `zoho_service.py` método `create_draft_invoice_from_payload()`, linha 317) não inclui campos de payment gateway nem email preferences.

Preciso que:
1. Investiga a Zoho Books API (https://www.zoho.com/books/api/v3/invoices/#create-an-invoice) para descobrir os campos corretos para:
   - Definir payment gateways (GoCardless + Stripe) — provavelmente `payment_options` ou `payment_gateways` no payload
   - Selecionar os emails de contacto — provavelmente `contact_persons` array com os IDs dos contatos

2. No `_build_draft_invoice_payload()`, adiciona estes campos ao payload dict:
   - Os payment gateways configurados
   - Os contact_persons do customer (vai precisar buscar via `get_contact_by_id()` que já existe no zoho_service.py linha 191, e extrair os `contact_person_id`s)

3. Se os IDs dos payment gateways precisarem ser configuráveis, adiciona como settings no `backend/app/models.py` (como já existe para tax IDs) em vez de hardcodar.

Nota: os payment gateways provavelmente precisam estar configurados na conta Zoho primeiro. O campo no payload pode ser algo como:
```json
{
  "payment_options": {
    "payment_gateways": [
      {"configured": true, "gateway_name": "gocardless"},
      {"configured": true, "gateway_name": "stripe"}
    ]
  }
}
```
```

---

## Dica de uso

Podes colar cada prompt individualmente no Claude Code (VS Code). Recomendo fazer pela ordem:
1. **#5** (mais simples — remover texto)
2. **#1** (adicionar keywords de tax)
3. **#4** (delivery fee — precisa investigação)
4. **#2** (unsync individual — backend + frontend)
5. **#3** (customer matching — pode precisar de testes com dados reais)
6. **#6** (payment gateways — depende da API Zoho)
