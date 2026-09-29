import React, { useEffect, useState } from "react";
import { App, Alert, Button, Descriptions, Drawer, Dropdown, Empty, Input, Segmented, Space, Table, Tag, Tooltip, Typography } from "antd";
import {
  AppstoreOutlined, CheckCircleOutlined, ClearOutlined, ClockCircleOutlined, EnvironmentOutlined, ExportOutlined,
  EyeOutlined, ReloadOutlined, SearchOutlined, StarFilled, StarOutlined, SyncOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";

const { Title, Text, Paragraph } = Typography;
const STATUSES = ["待筛选", "关注", "已投递", "笔试", "面试", "Offer", "暂不考虑"];
const COLORS = { 待筛选: "default", 关注: "gold", 已投递: "blue", 笔试: "cyan", 面试: "purple", Offer: "green", 暂不考虑: "red" };
const FOCUS = [{ value: 3, label: "必投" }, { value: 2, label: "重点关注" }, { value: 1, label: "可以冲" }];
const FACETS = [
  ["industry", "行业", "industry"], ["company_type", "企业性质", "company_type"],
  ["recruitment_type", "招聘类型", "recruitment_type"], ["location", "工作地点", "location"],
  ["education", "学历", "education"], ["exam", "笔试", "exam"], ["tag", "我的标签", "tags"],
];
const GROUPS = [
  ["", "不分组"], ["annotation_status", "跟进状态"], ["industry", "行业"],
  ["company_type", "企业性质"], ["recruitment_type", "招聘类型"],
  ["location", "工作地点"], ["education", "学历"],
];
const PAGE_SIZE = 30;
const display = (value) => value == null || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
const safeHref = (value) => typeof value === "string" && /^https?:\/\/\S+$/i.test(value.trim()) ? value.trim() : null;
const labelForFocus = (value) => FOCUS.find((item) => item.value === value)?.label || "关注";

async function request(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "job-board" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "请求失败，请稍后重试");
  return result;
}

function FacetRow({ title, values, selected, onChange, labels = {}, color = "default", limit = 14 }) {
  const [expanded, setExpanded] = useState(false);
  const [term, setTerm] = useState("");
  const [finding, setFinding] = useState(false);
  const entries = Object.entries(values || {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN"));
  const matching = term.trim() ? entries.filter(([value]) => (labels[value] || value).toLocaleLowerCase().includes(term.trim().toLocaleLowerCase())) : entries;
  const shown = expanded ? matching : matching.slice(0, limit);
  const visible = [
    ...(selected || []).filter((value) => !shown.some(([option]) => option === value)).map((value) => [value, values?.[value] || 0]),
    ...shown,
  ];
  return <div className="feishu-facet-row">
    <span className="feishu-facet-name">{title}</span>
    <div className="feishu-facet-main">
      <div className={`feishu-facet-options${expanded ? " expanded" : ""}`}>
        <Button type="text" className={!selected?.length ? "selected" : ""} onClick={() => onChange([])}>不限</Button>
        {(entries.length > limit || finding || term) && <div className={`feishu-facet-search-slot${finding || term ? " open" : ""}`}>
          {finding || term ? <Input size="small" autoFocus allowClear prefix={<SearchOutlined />} className="feishu-facet-search"
            placeholder={`查找${title}`} aria-label={`查找${title}筛选项`} value={term} onChange={(event) => setTerm(event.target.value)}
            onBlur={() => { if (!term) setFinding(false); }}
            onKeyDown={(event) => { if (event.key === "Escape") { setTerm(""); setFinding(false); } }} /> :
            <Tooltip title={`查找${title}`}><Button type="text" icon={<SearchOutlined />} aria-label={`查找${title}筛选项`}
              onClick={() => setFinding(true)} /></Tooltip>}
        </div>}
        {visible.map(([value, count]) => <Button type="text" key={value} className={selected?.includes(value) ? "selected" : ""}
          onClick={() => onChange(selected?.includes(value) ? selected.filter((item) => item !== value) : [...(selected || []), value])}>
          {color !== "default" && <span className={`feishu-dot feishu-dot-${color}`} />}{labels[value] || value}<small>{count}</small>
        </Button>)}
        {term && matching.length === 0 && <span className="feishu-facet-empty">没有匹配的筛选项</span>}
      </div>
      {matching.length > limit && <Button type="link" className="feishu-expand" onClick={() => setExpanded(!expanded)}>
        {expanded ? "收起" : `展开全部 ${matching.length} 项`}</Button>}
    </div>
  </div>;
}

export default function FeishuWorkspace() {
  const { message } = App.useApp();
  const [manifest, setManifest] = useState(null);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState({});
  const [filters, setFilters] = useState({});
  const [filterResetKey, setFilterResetKey] = useState(0);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("updated_date");
  const [direction, setDirection] = useState("desc");
  const [groupBy, setGroupBy] = useState("");
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query.trim()); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [query]);
  const loadManifest = async () => {
    try { setManifest(await request("/admin/feishu/manifest")); setError(""); }
    catch (reason) { setError(reason.message); }
  };
  useEffect(() => {
    loadManifest();
    const timer = setInterval(loadManifest, 30000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!manifest) return;
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({ q: search, sort, direction, group_by: groupBy, limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
    Object.entries(filters).forEach(([key, values]) => (values || []).forEach((value) => params.append(key, String(value))));
    request(`/admin/feishu/records?${params}`).then((result) => {
      if (!active) return;
      setRows(result.items || []); setTotal(result.total || 0); setFacets(result.facets || {}); setError("");
    }).catch((reason) => { if (active) setError(reason.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [!!manifest, manifest?.status?.last_run?.id, search, sort, direction, groupBy, page, filters, revision]);

  const setFacet = (key, values) => { setFilters((old) => ({ ...old, [key]: values })); setPage(1); };
  const reset = () => { setQuery(""); setSearch(""); setFilters({}); setFilterResetKey((value) => value + 1); setPage(1); };
  const sortOrder = (key) => sort === key ? direction === "asc" ? "ascend" : "descend" : null;
  const open = (row) => {
    setSelected(row);
    setDraft({ ...row.annotation, tags: (row.annotation?.tags || []).join("，") });
  };
  const save = async (row = selected, next = draft, close = true) => {
    if (!row || !next) return;
    setBusy(true);
    try {
      await request(`/admin/feishu/annotations/${encodeURIComponent(row.table_id)}/${encodeURIComponent(row.record_id)}`, "PUT", {
        status: next.status, priority: next.status === "关注" ? Number(next.priority) || 1 : 0,
        tags: String(next.tags || "").split(/[,，]/).map((value) => value.trim()).filter(Boolean), note: next.note || "",
      });
      message.success("跟进已保存");
      if (close) { setSelected(null); setDraft(null); }
      setRevision((value) => value + 1);
    } catch (reason) { message.error(reason.message); }
    finally { setBusy(false); }
  };
  const sync = async () => {
    setBusy(true);
    try { await request("/admin/feishu/sync", "POST"); message.success("已开始同步"); await loadManifest(); }
    catch (reason) { message.error(reason.message); }
    finally { setBusy(false); }
  };
  const groupedCell = (_, index) => {
    const value = rows[index]?.group_value || "未填写";
    if (index > 0 && (rows[index - 1]?.group_value || "未填写") === value) return { rowSpan: 0 };
    let count = 1;
    while (index + count < rows.length && (rows[index + count]?.group_value || "未填写") === value) count++;
    return { rowSpan: count };
  };
  const columns = [
    ...(groupBy ? [{ title: "分组", width: 118, fixed: "left", onCell: groupedCell,
      render: (_, row) => <Tag color="geekblue" className="feishu-clipped-tag">{row.group_value || "未填写"}</Tag> }] : []),
    { title: "公司 / 岗位", key: "company", width: 280, fixed: "left", sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("company"),
      render: (_, row) => <div className="feishu-company-cell">
        <Button type="link" onClick={() => open(row)}>{row.fields?.["公司名称"] || row.fields?.["企业名称"] || "未填写公司"}</Button>
        <Tooltip title={display(row.fields?.["校招岗位"] || row.fields?.["招聘岗位"] || row.fields?.["岗位"])}>
          <span className="feishu-two-line">{display(row.fields?.["校招岗位"] || row.fields?.["招聘岗位"] || row.fields?.["岗位"])}</span>
        </Tooltip>
      </div> },
    { title: "跟进", key: "status", width: 118, sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("status"),
      render: (_, row) => <Tag color={COLORS[row.annotation.status]}>{row.annotation.status}</Tag> },
    { title: "关注程度", key: "priority", width: 118, sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("priority"),
      render: (_, row) => <Dropdown trigger={["click"]} menu={{ items: FOCUS.map(({ value, label }) => ({ key: String(value), label })),
        onClick: ({ key }) => save(row, { ...row.annotation, tags: (row.annotation.tags || []).join("，"), status: "关注", priority: Number(key) }, false) }}>
        <Button type="text" className="feishu-focus-action" aria-label={`设置 ${row.fields?.["公司名称"] || "公司"} 的关注程度`}>
          {row.annotation.status === "关注" ? <StarFilled /> : <StarOutlined />}
          {row.annotation.status === "关注" ? labelForFocus(row.annotation.priority) : "加入关注"}
        </Button>
      </Dropdown> },
    { title: "行业 / 性质", key: "industry", width: 176, sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("industry"),
      render: (_, row) => <div className="feishu-small-tags">
        {row.fields?.["公司行业"] && <Tag color="cyan" className="feishu-clipped-tag">{row.fields["公司行业"]}</Tag>}
        {row.fields?.["企业性质"] && <Tag color="blue" className="feishu-clipped-tag">{row.fields["企业性质"]}</Tag>}
      </div> },
    { title: "地点", key: "location", width: 146, render: (_, row) => <Tooltip title={display(row.fields?.["工作地点"])}>
      <span className="feishu-two-line"><EnvironmentOutlined /> {display(row.fields?.["工作地点"])}</span></Tooltip> },
    { title: "招聘类型", width: 112, render: (_, row) => row.fields?.["招聘类型"] ? <Tag color="purple" className="feishu-clipped-tag">{row.fields["招聘类型"]}</Tag> : "—" },
    { title: "学历", width: 92, render: (_, row) => <span className="feishu-one-line">{display(row.fields?.["学历"])}</span> },
    { title: "网申截止", key: "deadline", width: 135, sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("deadline"), render: (_, row) => {
      const label = row.deadline?.label || "未填写";
      const special = row.deadline?.sort?.startsWith("0000");
      return <Tooltip title={row.deadline?.raw && row.deadline.raw !== label ? `统一格式：${label} · 原始值：${row.deadline.raw}` : label}>
        {special ? <Tag color="orange">{label}</Tag> : <span className="feishu-one-line">{label}</span>}
      </Tooltip>;
    } },
    { title: "更新日期", key: "updated_date", width: 136, sorter: true, sortDirections: ["ascend", "descend", "ascend"], sortOrder: sortOrder("updated_date"),
      render: (_, row) => <Text type="secondary">{display(row.fields?.["网申更新"])}</Text> },
    { title: "", key: "detail", width: 54, fixed: "right", render: (_, row) => <Tooltip title="查看详情与跟进">
      <Button type="text" icon={<EyeOutlined />} aria-label={`查看 ${row.fields?.["公司名称"] || "公司"} 详情`} onClick={() => open(row)} /></Tooltip> },
  ];
  const onTableChange = (_, __, sorter) => {
    if (!sorter?.columnKey) return;
    setSort(sorter.columnKey); setDirection(sorter.order === "ascend" ? "asc" : "desc"); setPage(1);
  };
  const activeCount = Object.values(filters).reduce((sum, values) => sum + (values?.length || 0), 0) + Number(!!query.trim());
  const last = manifest?.status?.last_run;

  return <div className="feishu-workspace">
    <div className="feishu-heading">
      <div><Title level={2}>飞书职位库 <Text className="feishu-count">{manifest?.status?.records?.toLocaleString() || "—"}</Text></Title>
        <Text type="secondary">{last?.finished ? `源表更新于 ${dayjs(last.finished).format("YYYY-MM-DD HH:mm")}` : "等待首次同步"} · 仅管理员可见</Text></div>
      <Space><Button type="text" icon={<ReloadOutlined />} onClick={() => { loadManifest(); setRevision((value) => value + 1); }}>刷新</Button>
        <Button type="text" icon={<SyncOutlined />} loading={busy || manifest?.status?.running} onClick={sync}>同步源表</Button></Space>
    </div>
    {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => { loadManifest(); setRevision((value) => value + 1); }}>重试</Button>} />}
    {last?.status === "error" && <Alert type="warning" showIcon message="上次同步失败，当前仍可使用已保存的数据" description={last.message} />}
    <div className="feishu-filter-panel">
      <div className="feishu-filter-toolbar">
        <Input size="large" prefix={<SearchOutlined />} placeholder="搜索公司、岗位、技术方向、地点或原始字段" allowClear value={query}
          onChange={(event) => setQuery(event.target.value)} aria-label="搜索职位" />
        <Button className="feishu-clear-filters" icon={<ClearOutlined />} onClick={reset}>
          清除筛选{activeCount ? ` (${activeCount})` : ""}</Button>
      </div>
      <FacetRow key={`status-${filterResetKey}`} title="投递进度" values={Object.fromEntries(STATUSES.map((value) => [value, facets.annotation_status?.[value] || 0]))}
        selected={filters.status} onChange={(values) => setFacet("status", values)} color="status" limit={20} />
      <FacetRow key={`focus-${filterResetKey}`} title="关注程度" values={Object.fromEntries(FOCUS.map(({ value }) => [String(value), facets.focus?.[value] || 0]))}
        labels={Object.fromEntries(FOCUS.map(({ value, label }) => [String(value), label]))}
        selected={filters.focus} onChange={(values) => setFacet("focus", values)} color="focus" limit={20} />
      {FACETS.map(([key, label, source]) => <FacetRow key={`${key}-${filterResetKey}`} title={label} values={facets[source]}
        selected={filters[key]} onChange={(values) => setFacet(key, values)} />)}
    </div>
    <div className="feishu-list-heading">
      <div><strong>职位清单</strong><span>{total.toLocaleString()} 条结果</span></div>
      <div className="feishu-group-switch">
        <span><AppstoreOutlined /> 分组查看</span>
        <div className="feishu-group-scroll"><Segmented value={groupBy} options={GROUPS.map(([value, label]) => ({ value, label }))}
          onChange={(value) => { setGroupBy(value); setPage(1); }} /></div>
      </div>
    </div>
    <Table className="feishu-table" rowKey={(row) => `${row.table_id}/${row.record_id}`} dataSource={rows} columns={columns}
      loading={loading} size="middle" tableLayout="fixed" onChange={onTableChange} scroll={{ x: groupBy ? 1470 : 1352 }}
      locale={{ emptyText: <Empty description="没有找到符合条件的职位" /> }}
      pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: setPage,
        showTotal: (count, range) => `${range[0]}–${range[1]} / ${count.toLocaleString()}` }} />

    <Drawer className="feishu-detail-drawer" open={!!selected} onClose={() => { setSelected(null); setDraft(null); }} width={740}
      title={selected ? selected.fields?.["公司名称"] || selected.fields?.["企业名称"] || "职位详情" : "职位详情"}>
      {selected && draft && <>
        <Space wrap className="feishu-detail-tags"><Tag color={COLORS[draft.status]}>{draft.status === "关注" ? labelForFocus(draft.priority) : draft.status}</Tag>
          {selected.fields?.["公司行业"] && <Tag color="cyan">{selected.fields["公司行业"]}</Tag>}
          {selected.fields?.["招聘类型"] && <Tag color="purple">{selected.fields["招聘类型"]}</Tag>}</Space>
        <Title level={5}>招聘岗位</Title><Paragraph className="feishu-position-detail">{display(selected.fields?.["校招岗位"] || selected.fields?.["招聘岗位"] || selected.fields?.["岗位"])}</Paragraph>
        <Space wrap className="feishu-apply-links">
          {[["网申公告", selected.fields?.["网申公告"]], ["投递链接", selected.fields?.["投递链接"]]].map(([label, value]) => safeHref(value) ?
            <Button key={label} href={safeHref(value)} target="_blank" rel="noopener noreferrer" icon={<ExportOutlined />}>{label}</Button> : null)}
        </Space>
        <section className="feishu-follow-panel"><Title level={5}>我的投递进度</Title>
          <div className="feishu-follow-field"><label>进度</label><div className="feishu-follow-options">{STATUSES.map((value) => <Button key={value} type="text" className={draft.status === value ? "selected" : ""}
            onClick={() => setDraft({ ...draft, status: value, priority: value === "关注" ? draft.priority || 1 : 0 })}>{value}</Button>)}</div></div>
          {draft.status === "关注" && <div className="feishu-follow-field"><label>关注程度</label><div className="feishu-follow-options">{FOCUS.map(({ value, label }) => <Button key={value} type="text" className={draft.priority === value ? "selected" : ""}
            onClick={() => setDraft({ ...draft, priority: value })}><StarFilled /> {label}</Button>)}</div></div>}
          <div className="feishu-follow-field"><label>标签</label><Input placeholder="用逗号分隔，如内推、有笔试" value={draft.tags} onChange={(event) => setDraft({ ...draft, tags: event.target.value })} /></div>
          <div className="feishu-follow-field"><label>备注</label><Input.TextArea rows={4} placeholder="记录投递渠道、联系人、薪资或下一步" value={draft.note} onChange={(event) => setDraft({ ...draft, note: event.target.value })} /></div>
          <Button type="primary" loading={busy} onClick={() => save()} icon={<CheckCircleOutlined />}>保存跟进</Button>
        </section>
        <div className="feishu-raw-heading"><Title level={5}>完整资料</Title><Text type="secondary">来自飞书原始表格</Text></div>
        <Descriptions bordered size="small" column={1} items={Object.entries(selected.fields || {}).map(([key, value]) => ({ key, label: key,
          children: safeHref(value) ? <a href={safeHref(value)} target="_blank" rel="noopener noreferrer">打开链接 <ExportOutlined /></a> :
            <span className="feishu-field-value">{display(value)}</span> }))} />
      </>}
    </Drawer>
  </div>;
}
