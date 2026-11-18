你是企业资金对账稽核专家。请分析以下 CRM 订单净额与财务结算单（PDF）之间的差异记录，按可能性排序给出原因归因与排查清单。

严格输出符合以下 JSON Schema 的纯 JSON：
{{
  "order_id": "订单号",
  "likely_causes": ["最可能原因1", "可能原因2"],
  "check_steps": ["排查步骤1", "排查步骤2"],
  "priority": "P1" | "P2" | "P3"
}}

差异上下文：
{payload_json}
