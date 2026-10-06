<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
const tree = ref<any[]>([])
const data = ref<any>(null)
const shortages = ref<any[]>([])
const stats = ref<any>({})
const orders = ref<any[]>([])
const picked = ref<number[]>([])
const errorMsg = ref('')

const heading = computed(() => {
  if (!data.value) return ''
  if (data.value.merged)
    return '合单 · ' + data.value.orders.map((o: any) => `${o.code} · ${o.outlet}`).join(' ＋ ')
  return `${data.value.order?.code} · ${data.value.order?.outlet}`
})

function errText(e: any): string {
  try {
    const j = JSON.parse(e?.message || '')
    return j.detail || e?.message || '未知错误'
  } catch { return e?.message || '未知错误' }
}

function toggle(id: number) {
  errorMsg.value = ''
  const i = picked.value.indexOf(id)
  if (i >= 0) picked.value.splice(i, 1)
  else {
    if (picked.value.length >= 2) picked.value.shift()
    picked.value.push(id)
  }
}

async function run() {
  errorMsg.value = ''
  const oid = picked.value[0] ?? 1
  try {
    data.value = await api('/prep/run?order_id=' + oid, { method: 'POST' })
    const res = await api('/prep/shortages?order_id=' + oid)
    shortages.value = res.shortages || []
    stats.value = res.stats || {}
  } catch (e: any) {
    errorMsg.value = '生成备料单失败：' + errText(e)
  }
}

async function merge() {
  errorMsg.value = ''
  if (picked.value.length !== 2) {
    errorMsg.value = '合单需要正好两家门店订单，请先在顶栏点选两家'
    return
  }
  try {
    const res = await api('/prep/merge', {
      method: 'POST',
      body: JSON.stringify({ order_ids: picked.value }),
    })
    data.value = res
    shortages.value = res.shortages || []
    stats.value = res.stats || {}
  } catch (e: any) {
    data.value = null
    shortages.value = []
    stats.value = {}
    errorMsg.value = '合单失败，两边均未落新行：' + errText(e)
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
  <p class="sub">左 BOM 树 · 中备料表 · 右缺料便利贴 · 顶栏点选两家门店订单可合单</p>
  <div class="kp-chips" style="margin-bottom:0.75rem" v-if="orders.length">
    <span
      v-for="o in orders"
      :key="o.id"
      class="kp-chip"
      :class="{ picked: picked.includes(o.id) }"
      style="cursor:pointer"
      @click="toggle(o.id)"
    >
      {{ o.code }} · {{ o.outlet }}
    </span>
  </div>
  <div style="display:flex;gap:0.5rem;align-items:center">
    <button class="btn" @click="run">生成备料单</button>
    <button class="btn" :disabled="picked.length !== 2" @click="merge">两店合单备料</button>
  </div>
  <p v-if="errorMsg" class="kp-error">{{ errorMsg }}</p>
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
      <h2>备料单 · {{ heading }}</h2>
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
      <h2>⚠ 缺料便利贴<template v-if="stats.shortage_count !== undefined"> · {{ stats.shortage_count }} 项 · 共 {{ stats.total_shortage_qty }}</template></h2>
      <div v-for="r in shortages" :key="r.ingredient_id" class="kp-shortage-item">
        <span>{{ r.ingredient_name }}</span>
        <span class="kp-qty">−{{ r.shortage }} {{ r.unit }}</span>
      </div>
      <p v-if="!shortages.length" style="font-size:0.8rem;margin:0.5rem 0 0">暂无缺料</p>
    </aside>
  </div>
</template>
