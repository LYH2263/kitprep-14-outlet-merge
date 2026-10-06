<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const tree = ref<any[]>([])
const data = ref<any>(null)
const shortages = ref<any[]>([])
const orders = ref<any[]>([])
const selected = ref<number[]>([])
const merged = ref<any>(null)
const mergeError = ref('')
const merging = ref(false)

async function run() {
  data.value = await api('/prep/run?order_id=1', { method: 'POST' })
  try {
    const res = await api('/prep/shortages?order_id=1')
    shortages.value = res.shortages || []
  } catch { shortages.value = [] }
}

function toggle(id: number) {
  mergeError.value = ''
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else if (selected.value.length < 2) selected.value.push(id)
}

async function mergeRun() {
  // 空名单或只选一家：前端按拒绝处理，不发请求
  if (selected.value.length !== 2) {
    mergeError.value = '合单必须恰好选择两家不同门店'
    return
  }
  merging.value = true
  mergeError.value = ''
  const previous = merged.value
  try {
    // 行、缺料贴、统计由后端同一份结果产出并对账；对不上整次失败，这里不留半成品
    merged.value = await api('/prep/merge', {
      method: 'POST',
      body: JSON.stringify({ order_ids: selected.value }),
    })
  } catch (e: any) {
    merged.value = previous  // 失败：已有单不动，界面也不留半份结果
    mergeError.value = '合单失败（两边均未落单）：' + (e.message || '未知错误')
  } finally {
    merging.value = false
  }
}

onMounted(async () => {
  tree.value = await api('/bom/tree')
  orders.value = await api('/orders')
  await run()
})
</script>
<template>
  <h1>备料工作台</h1>
  <p class="sub">左 BOM 树 · 中备料表 · 右缺料便利贴 · 顶栏点选两家门店可合单</p>
  <div class="kp-chips" style="margin-bottom:0.75rem">
    <span v-for="o in orders" :key="o.id" class="kp-chip"
          :style="{ cursor: 'pointer', outline: selected.includes(o.id) ? '2px solid var(--kp-accent)' : 'none' }"
          @click="toggle(o.id)">
      {{ o.code }} · {{ o.outlet }}
    </span>
  </div>
  <div style="display:flex;gap:0.6rem;align-items:center;flex-wrap:wrap">
    <button class="btn" @click="run">单店生成备料单（{{ data?.order?.code || '订单1' }}）</button>
    <button class="btn" :disabled="selected.length !== 2 || merging" @click="mergeRun">
      {{ merging ? '合单中…' : `两店合单（已选 ${selected.length}/2）` }}
    </button>
    <span v-if="selected.length === 1" style="font-size:0.8rem;color:var(--kp-accent)">只选一家按拒绝处理，请再选一家不同门店</span>
    <span v-if="mergeError" class="badge badge-bad" style="font-size:0.8rem">{{ mergeError }}</span>
  </div>

  <div v-if="merged" class="card" style="margin-top:0.85rem;padding:0.85rem 1rem">
    <h2 style="margin:0 0 0.35rem">合单备料 ·
      <span v-for="o in merged.orders" :key="o.id">{{ o.code }}（{{ o.outlet }}） </span>
    </h2>
    <table>
      <thead>
        <tr><th>原料编码</th><th>原料</th>
        <th v-for="o in merged.orders" :key="o.id">{{ o.outlet }}需求</th>
        <th>合计需求</th><th>库存</th><th>缺料</th><th>单位</th></tr>
      </thead>
      <tbody>
        <tr v-for="l in merged.prep_lines" :key="l.ingredient_id">
          <td>{{ l.ingredient_code }}</td>
          <td>{{ l.ingredient_name }}</td>
          <td v-for="o in merged.orders" :key="o.id">
            {{ l.stores.find((s:any) => s.order_id === o.id)?.need_qty ?? 0 }}
          </td>
          <td><strong>{{ l.need_qty }}</strong></td>
          <td>{{ l.stock_qty }}</td>
          <td><span :class="['badge', l.shortage > 0 ? 'badge-bad' : 'badge-ok']">{{ l.shortage }}</span></td>
          <td>{{ l.unit }}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <td colspan="2">统计</td>
          <td v-for="r in merged.stats.per_store" :key="r.order_id">{{ r.need_qty }}</td>
          <td>{{ merged.stats.total_need_qty }}</td>
          <td></td>
          <td>{{ merged.stats.total_shortage_qty }}（{{ merged.stats.shortage_count }} 种）</td>
          <td></td>
        </tr>
      </tfoot>
    </table>
    <div class="kp-shortage-sticky" style="margin-top:0.75rem;max-width:420px;transform:none">
      <h2>⚠ 合单缺料贴 · 按两店加总 · {{ merged.stats.shortage_count }} 种</h2>
      <div v-for="r in merged.shortages" :key="r.ingredient_id" class="kp-shortage-item">
        <span>{{ r.ingredient_code }} · {{ r.ingredient_name }}</span>
        <span class="kp-qty">−{{ r.shortage }} {{ r.unit }}</span>
      </div>
      <p v-if="!merged.shortages.length" style="font-size:0.8rem;margin:0.5rem 0 0">暂无缺料</p>
    </div>
  </div>

  <div class="kp-workbench" style="margin-top:0.85rem">
    <aside class="kp-bom-tree">
      <h2>菜品 / BOM</h2>
      <div v-for="d in tree" :key="d.code" class="kp-dish-node">
        <strong>{{ d.dish }}</strong>
        <span style="font-size:0.7rem;color:#8a8078">{{ d.code }}</span>
        <ul>
          <li v-for="(c,i) in d.children" :key="i">{{ c.ingredient }} · {{ c.qty }} {{ c.unit }}</li>
        </ul>
      </div>
    </aside>
    <section class="kp-worksheet" v-if="data">
      <h2>备料单 · {{ data.order?.code }} · {{ data.order?.outlet }}</h2>
      <table>
        <thead><tr><th>原料</th><th>需求</th><th>库存</th><th>单位</th></tr></thead>
        <tbody>
          <tr v-for="l in data.prep_lines" :key="l.ingredient_id">
            <td>{{ l.ingredient_name }}</td><td>{{ l.need_qty }}</td><td>{{ l.stock_qty }}</td><td>{{ l.unit }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <aside class="kp-shortage-sticky">
      <h2>⚠ 缺料便利贴</h2>
      <div v-for="r in shortages" :key="r.ingredient_id" class="kp-shortage-item">
        <span>{{ r.ingredient_name }}</span>
        <span class="kp-qty">−{{ r.shortage }} {{ r.unit }}</span>
      </div>
      <p v-if="!shortages.length" style="font-size:0.8rem;margin:0.5rem 0 0">暂无缺料</p>
    </aside>
  </div>
</template>
