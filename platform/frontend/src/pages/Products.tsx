import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Product } from '../api'
import { PageHeader, StatusBadge, yuan } from '../ui'

/**
 * 冬款库存预警的判定阈值。
 *
 * 只有"**卖得动 + 快没货了**"才值得预警：动销为 0 的款库存低是正常清仓，
 * 库存高的款动销再好也不急。两个条件缺一个，这个列表就会变成噪音，
 * 而噪音的直接后果是没人再看它。
 */
const LOW_STOCK = 20
const MIN_SOLD = 5

function isWinter(product: Product): boolean {
  return !!product.season && /冬/.test(product.season)
}

export default function Products() {
  const [items, setItems] = useState<Product[]>([])
  const [total, setTotal] = useState(0)
  const [categories, setCategories] = useState<{ value: string; count: number }[]>([])
  const [seasons, setSeasons] = useState<{ value: string; count: number }[]>([])
  const [category, setCategory] = useState('')
  const [season, setSeason] = useState('')
  const [keyword, setKeyword] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .productFacets()
      .then((f) => {
        setCategories(f.categories)
        setSeasons(f.seasons)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => {
      api
        .products({
          page_size: 50,
          category: category || undefined,
          season: season || undefined,
          q: keyword || undefined,
        })
        .then((page) => {
          setItems(page.items)
          setTotal(page.total)
          setError(null)
        })
        .catch((e: Error) => setError(e.message))
    }, 220) // 输入防抖：别每敲一个字就打一次接口
    return () => clearTimeout(timer)
  }, [category, season, keyword])

  // 冬款预警。**只在当前这批数据里算**，不另外打接口 ——
  // 它是"这一页里哪些要注意"，不是全库统计（全库那个在 /inventory/low-stock）。
  const winterRisk = useMemo(
    () =>
      items
        .filter((p) => isWinter(p) && p.stock <= LOW_STOCK && p.sold_count >= MIN_SOLD)
        .sort((a, b) => b.sold_count - a.sold_count),
    [items],
  )
  const winterTotal = useMemo(() => items.filter(isWinter).length, [items])

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '商品库']}
        title="商品库"
        badge={<span className="tag">共 {total} 个 SPU</span>}
        desc="选品 Agent 会按类目 + 季节 + 风格标签找相似竞品，再叠加库存硬约束过滤。毛利率口径：毛利额 = 吊牌价 − 成本，毛利率 = 毛利额 ÷ 吊牌价。"
      />

      {error && <div className="error-box">{error}</div>}

      {winterRisk.length > 0 && (
        <div className="card" style={{ marginBottom: 14, borderColor: 'var(--danger)' }}>
          <h3>
            冬款库存预警 <span className="badge danger">{winterRisk.length} 个款</span>
            <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
              当季共 {winterTotal} 个冬款
            </span>
          </h3>
          <p className="muted" style={{ margin: '4px 0 10px', fontSize: 12.5 }}>
            判定条件是「近 30 天动销 ≥ {MIN_SOLD} 件 且 可售 ≤ {LOW_STOCK} 件」——
            两个条件缺一个就会变成噪音：不动销的低库存是清仓，库存充足的爆款不急。
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>SPU</th>
                  <th>标题</th>
                  <th>近 30 天动销</th>
                  <th>可售库存</th>
                  <th style={{ width: 120 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {winterRisk.slice(0, 8).map((p) => (
                  <tr key={p.id}>
                    <td className="mono">{p.spu_code}</td>
                    <td>{p.title}</td>
                    <td>
                      <b>{p.sold_count}</b> 件
                    </td>
                    <td>
                      <span className="badge danger">{p.stock}</span>
                    </td>
                    <td>
                      {/* 跨模块联动：带着上下文跳到任务工作台，而不是让人自己再敲一遍需求 */}
                      <Link
                        className="button small"
                        to="/tasks"
                        state={{ draft: `${p.spu_code} ${p.title} 库存仅剩 ${p.stock} 件、近 30 天动销 ${p.sold_count} 件，请评估补货数量并给出补货建议` }}
                      >
                        建补货任务
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="toolbar">
        <input
          type="text"
          placeholder="搜索标题…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{ width: 190 }}
        />
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">全部类目</option>
          {categories.map((c) => (
            <option key={c.value} value={c.value}>
              {c.value}（{c.count}）
            </option>
          ))}
        </select>
        <select value={season} onChange={(e) => setSeason(e.target.value)}>
          <option value="">全部季节</option>
          {seasons.map((s) => (
            <option key={s.value} value={s.value}>
              {s.value}（{s.count}）
            </option>
          ))}
        </select>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>SPU</th>
                <th>标题</th>
                <th>类目</th>
                <th>季节</th>
                <th>可售库存</th>
                <th>近 30 天动销</th>
                <th>风格标签</th>
                <th>成本</th>
                <th>吊牌价</th>
                <th>毛利额</th>
                <th>毛利率</th>
                <th>评分</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {items.map((product) => {
                const cost = Number(product.cost_price ?? 0)
                const price = Number(product.list_price ?? 0)
                const margin = price > 0 ? (1 - cost / price) * 100 : null
                return (
                  <tr key={product.id}>
                    <td className="mono">{product.spu_code}</td>
                    <td>{product.title}</td>
                    <td className="muted">
                      {product.category}
                      {product.sub_category ? ` / ${product.sub_category}` : ''}
                    </td>
                    <td>{product.season ?? '—'}</td>
                    <td>
                      {/* 库存是**流水推出来的**，不是一个余额字段 —— 见后端 Inventory 的注释 */}
                      <span className={`badge ${product.stock === 0 ? 'danger' : product.stock <= LOW_STOCK ? 'warn' : 'ok'}`}>
                        {product.stock}
                      </span>
                    </td>
                    <td className="muted">{product.sold_count}</td>
                    <td className="muted">{(product.style_tags ?? []).join(' · ') || '—'}</td>
                    <td>{yuan(product.cost_price)}</td>
                    <td>{yuan(product.list_price)}</td>
                    <td>{yuan(price - cost)}</td>
                    <td>
                      {margin === null ? (
                        '—'
                      ) : (
                        <span className={`badge ${margin < 55 ? 'warn' : 'ok'}`}>{margin.toFixed(1)}%</span>
                      )}
                    </td>
                    <td>{product.rating ?? '—'}</td>
                    <td>
                      <StatusBadge status={product.status} />
                    </td>
                  </tr>
                )
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={13} className="empty">
                    没有匹配的商品
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
