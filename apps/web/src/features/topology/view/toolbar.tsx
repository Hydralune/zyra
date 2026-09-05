import { useMemo } from "react"
import type {
  TopologyControllerSnapshot,
  TopologyLayerId,
  TopologyStatusFilter,
} from "./contracts.ts"
import type { TopologyWorkbenchController } from "./controller.ts"
import { topologyFilterFacets } from "./filtering.ts"
import { topologyLayerIds, topologyLayerLabel } from "./layers.ts"

const STATUS_OPTIONS: readonly {
  value: TopologyStatusFilter
  label: string
}[] = Object.freeze([
  { value: "all", label: "全部状态" },
  { value: "active", label: "执行中" },
  { value: "waiting", label: "等待 / 受阻" },
  { value: "recovering", label: "恢复中" },
  { value: "failed", label: "失败 / 拒绝" },
  { value: "terminal", label: "已结束" },
  { value: "changed", label: "关系已变更" },
  { value: "policy-risk", label: "策略风险" },
])

function toggle(values: readonly string[], value: string): readonly string[] {
  const result = new Set(values)
  if (result.has(value)) result.delete(value)
  else result.add(value)
  return Object.freeze([...result].sort())
}
function FilterMenu({
  label,
  values,
  selected,
  onChange,
}: {
  label: string
  values: readonly string[]
  selected: readonly string[]
  onChange(value: readonly string[]): void
}) {
  if (values.length === 0) return null
  return (
    <details className="topology-filter-menu">
      <summary>
        {label}
        {selected.length > 0 ? <span className="filter-count">{selected.length}</span> : null}
      </summary>
      <div className="topology-filter-options">
        {values.slice(0, 80).map((value) => (
          <label key={value}>
            <input
              type="checkbox"
              checked={selected.includes(value)}
              onChange={() => onChange(toggle(selected, value))}
            />
            <span>{value}</span>
          </label>
        ))}
        {values.length > 80 ? <p>{values.length - 80} additional facets hidden.</p> : null}
      </div>
    </details>
  )
}

export function TopologyToolbar({
  controller,
  snapshot,
}: {
  controller: TopologyWorkbenchController
  snapshot: TopologyControllerSnapshot
}) {
  const facets = useMemo(() => topologyFilterFacets(snapshot.model), [snapshot.model])
  const filters = snapshot.filters
  return (
    <div className="topology-toolbar" aria-label="Topology graph controls">
      <div className="topology-toolbar-primary">
        <label className="topology-search">
          <span className="visually-hidden">搜索关系图</span>
          <input
            type="search"
            value={filters.query}
            placeholder="搜索节点、路由、检查点…"
            onChange={(event) => controller.setFilters({ query: event.currentTarget.value })}
            aria-controls="topology-graph-region"
          />
          {filters.query ? (
            <button
              type="button"
              className="icon-button"
              aria-label="清除搜索"
              onClick={() => controller.setFilters({ query: "" })}
            >
              ×
            </button>
          ) : null}
        </label>
        <label className="topology-select-control">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) =>
              controller.setFilters({
                status: event.currentTarget.value as TopologyStatusFilter,
              })
            }
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="topology-select-control">
          <span>图层</span>
          <select
            value={snapshot.activeLayer}
            onChange={(event) =>
              controller.setLayer(event.currentTarget.value as TopologyLayerId)
            }
          >
            {topologyLayerIds().map((id) => (
              <option key={id} value={id}>
                {topologyLayerLabel(id)}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="button button-secondary" onClick={() => controller.fit()}>
          适应画布
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => controller.viewport.zoomAt(
            {
              x: snapshot.viewport.viewport.width / 2,
              y: snapshot.viewport.viewport.height / 2,
            },
            snapshot.viewport.transform.scale * 1.25,
            "programmatic",
          )}
          aria-label="放大"
        >
          +
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => controller.viewport.zoomAt(
            {
              x: snapshot.viewport.viewport.width / 2,
              y: snapshot.viewport.viewport.height / 2,
            },
            snapshot.viewport.transform.scale / 1.25,
            "programmatic",
          )}
          aria-label="缩小"
        >
          −
        </button>
      </div>
      <details className="topology-extra-filters"><summary>更多筛选</summary><div className="topology-toolbar-filters">
        <FilterMenu
          label="命名空间"
          values={facets.namespaces}
          selected={filters.namespaces}
          onChange={(namespaces) => controller.setFilters({ namespaces })}
        />
        <FilterMenu
          label="角色"
          values={facets.roles}
          selected={filters.roles}
          onChange={(roles) => controller.setFilters({ roles })}
        />
        <FilterMenu
          label="执行位置"
          values={facets.locations}
          selected={filters.locations}
          onChange={(locations) =>
            controller.setFilters({
              locations: locations as typeof filters.locations,
            })
          }
        />
        <FilterMenu
          label="服务商"
          values={facets.providers}
          selected={filters.providers}
          onChange={(providers) => controller.setFilters({ providers })}
        />
        <FilterMenu
          label="模型"
          values={facets.models}
          selected={filters.models}
          onChange={(models) => controller.setFilters({ models })}
        />
        <FilterMenu
          label="隐私等级"
          values={facets.privacyClasses}
          selected={filters.privacyClasses}
          onChange={(privacyClasses) => controller.setFilters({ privacyClasses })}
        />
        <FilterMenu
          label="能力"
          values={facets.capabilities}
          selected={filters.capabilities}
          onChange={(capabilities) => controller.setFilters({ capabilities })}
        />
        <label className="topology-filter-check">
          <input
            type="checkbox"
            checked={filters.changedOnly}
            onChange={(event) => controller.setFilters({ changedOnly: event.currentTarget.checked })}
          />
          仅显示变更
        </label>
        <label className="topology-filter-check">
          <input
            type="checkbox"
            checked={filters.openWorldOnly}
            onChange={(event) => controller.setFilters({ openWorldOnly: event.currentTarget.checked })}
          />
          动态新增
        </label>
        <label className="topology-filter-check">
          <input
            type="checkbox"
            checked={filters.policyViolationsOnly}
            onChange={(event) =>
              controller.setFilters({ policyViolationsOnly: event.currentTarget.checked })
            }
          />
          策略违规
        </label>
        <label className="topology-filter-check">
          <input
            type="checkbox"
            checked={filters.includePending}
            onChange={(event) => controller.setFilters({ includePending: event.currentTarget.checked })}
          />
          包含待处理
        </label>
        <button type="button" className="link-button" onClick={() => controller.resetFilters()}>
          重置筛选
        </button>
      </div></details>
    </div>
  )
}
