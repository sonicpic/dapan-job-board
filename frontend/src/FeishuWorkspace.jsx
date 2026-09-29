import React, { useEffect, useState } from "react";
import {
  App, Alert, Button, Card, Col, Descriptions, Drawer, Empty, Input, Row,
  Select, Space, Table, Tag, Tooltip, Typography,
} from "antd";
import {
  ArrowRightOutlined, BankOutlined, BookOutlined, CheckCircleOutlined,
  ClockCircleOutlined, EditOutlined, EnvironmentOutlined, ExportOutlined,
  FilterOutlined, ReloadOutlined, SearchOutlined, SortAscendingOutlined,
  SortDescendingOutlined, StarFilled, StarOutlined, SyncOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";

const { Title, Text, Paragraph } = Typography;
const PAGE_SIZE = 30;
const STATUSES = ["待筛选", "关注", "已投递", "笔试", "面试", "Offer", "暂不考虑"];
const STATUS_COLORS = {
  待筛选: "default", 关注: "gold", 已投递: "blue", 笔试: "cyan",
  面试: "purple", Offer: "green", 暂不考虑: "red",
};
const FIELDS = [
  ["industry", "行业", "industry"],
  ["companyType", "企业性质", "company_type"],
  ["recruitmentType", "招聘类型", "recruitment_type"],
  ["location", "工作地点", "location"],
  ["education", "学历", "education"],
  ["exam", "笔试", "exam"],
  ["tag", "我的标签", "tags"],
];
const GROUPS = [
  ["annotation_status", "跟进状态"], ["industry", "行业"],
  ["company_type", "企业性质"], ["recruitment_type", "招聘类型"],
  ["location", "工作地点"], ["education", "学历"],
];
const SORTS = [
  ["updated_date", "更新时间"], ["deadline", "截止时间"],
  ["company", "公司名称"], ["position", "招聘岗位"],
  ["priority", "我的优先级"], ["status", "跟进状态"],
];
const display = (value) => value == null || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
const safeHref = (value) => typeof value === "string" && /^https?:\/\/\S+$/i.test(value.trim()) ? value.trim() : null;
const dateText = (value) => value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "尚未同步";

async function request(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "job-board" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.detail || "请求失败，请稍后重试");
    error.status = response.status;
    throw error;
  }
  return result;
}

export default function FeishuWorkspace() {
  const { message } = App.useApp();
  const [manifest, setManifest] = useState(null);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState({});
  const [statusCounts, setStatusCounts] = useState({});
  const [filters, setFilters] = useState({});
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState(-1);
  const [sort, setSort] = useState("updated_date");
  const [direction, setDirection] = useState("desc");
  const [groupBy, setGroupBy] = useState("");
  const [tableId, setTableId] = useState("");
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [moreFilters, setMoreFilters] = useState(false);
  const [selected, setSelected] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query.trim()); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const loadManifest = async () => {
    try {
      const result = await request("/admin/feishu/manifest");
      setManifest(result);
      setError("");
    } catch (reason) { setError(reason.message); }
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
    const params = new URLSearchParams({
      table_id: tableId, q: search, status, priority: String(priority),
      industry: filters.industry || "", company_type: filters.companyType || "",
      recruitment_type: filters.recruitmentType || "", location: filters.location || "",
      education: filters.education || "", exam: filters.exam || "", tag: filters.tag || "",
      sort, direction, group_by: groupBy, limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE),
    });
    request(`/admin/feishu/records?${params}`).then((result) => {
      if (!active) return;
      setRows(result.items || []);
      setTotal(result.total || 0);
      setFacets(result.facets || {});
      if (!status) setStatusCounts(result.facets?.annotation_status || {});
      setError("");
    }).catch((reason) => { if (active) setError(reason.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [!!manifest, manifest?.status?.last_run?.id, tableId, search, status, priority,
    filters, sort, direction, groupBy, page, revision]);

  const updateFilter = (key, value) => { setFilters((old) => ({ ...old, [key]: value || "" })); setPage(1); };
  const reset = () => {
    setQuery(""); setSearch(""); setStatus(""); setPriority(-1); setFilters({});
    setTableId(""); setGroupBy(""); setPage(1);
  };
  const options = (key) => Object.entries(facets[key] || {})
    .sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, label: `${value} · ${count}` }));
  const open = (row) => {
    setSelected(row);
    setDraft({ ...row.annotation, tags: (row.annotation?.tags || []).join("，") });
  };
  const save = async (row = selected, next = draft, close = true) => {
    if (!row || !next) return;
    setSaving(true);
    try {
      await request(`/admin/feishu/annotations/${encodeURIComponent(row.table_id)}/${encodeURIComponent(row.record_id)}`, "PUT", {
        status: next.status, priority: Number(next.priority),
        tags: String(next.tags || "").split(/[,，]/).map((value) => value.trim()).filter(Boolean),
        note: next.note || "",
      });
      message.success("跟进已保存");
      if (close) { setSelected(null); setDraft(null); }
      setRevision((value) => value + 1);
    } catch (reason) { message.error(reason.message); }
    finally { setSaving(false); }
  };
  const sync = async () => {
    setSyncing(true);
    try { await request("/admin/feishu/sync", "POST"); message.success("已开始同步飞书资料库"); await loadManifest(); }
    catch (reason) { message.error(reason.message); }
    finally { setSyncing(false); }
  };
  const groupedCell = (_, index) => {
    const value = rows[index]?.group_value || "未填写";
    if (index > 0 && (rows[index - 1]?.group_value || "未填写") === value) return { rowSpan: 0 };
    let rowSpan = 1;
    while (index + rowSpan < rows.length && (rows[index + rowSpan]?.group_value || "未填写") === value) rowSpan++;
    return { rowSpan };
  };
  const columns = [
    ...(groupBy ? [{ title: "分组", width: 136, fixed: "left", onCell: groupedCell,
      render: (_, row) => <Tag className="feishu-group-tag" color={groupBy === "annotation_status" ? STATUS_COLORS[row.group_value] : "geekblue"}>{row.group_value || "未填写"}</Tag> }] : []),
    { title: "公司 / 招聘岗位", width: 350, fixed: "left", render: (_, row) => <div className="feishu-company-cell">
      <Button type="link" className="feishu-company-link" onClick={() => open(row)}>{row.fields?.["公司名称"] || row.fields?.["企业名称"] || "未填写公司"}</Button>
      <Paragraph ellipsis={{ rows: 2, tooltip: row.fields?.["校招岗位"] || row.fields?.["招聘岗位"] || "" }}>
        {display(row.fields?.["校招岗位"] || row.fields?.["招聘岗位"] || row.fields?.["岗位"])}
      </Paragraph>
    </div> },
    { title: "跟进", width: 125, render: (_, row) => <Tag className="feishu-status-tag" color={STATUS_COLORS[row.annotation.status] || "default"}>{row.annotation.status}</Tag> },
    { title: "优先级", width: 108, render: (_, row) => <Button type="text" className="feishu-star-button" loading={saving} aria-label={`调整 ${row.fields?.["公司名称"] || "公司"} 优先级`} onClick={() => save(row, { ...row.annotation, tags: (row.annotation.tags || []).join("，"), priority: (row.annotation.priority + 1) % 4 }, false)}>
      {row.annotation.priority ? <StarFilled /> : <StarOutlined />}<span>{row.annotation.priority ? `${row.annotation.priority} 星` : "标重点"}</span>
    </Button> },
    { title: "行业 / 性质", width: 215, render: (_, row) => <Space size={4} wrap>
      {row.fields?.["公司行业"] && <Tag color="cyan">{row.fields["公司行业"]}</Tag>}
      {row.fields?.["企业性质"] && <Tag color="blue">{row.fields["企业性质"]}</Tag>}
    </Space> },
    { title: "地点", width: 145, render: (_, row) => <Text><EnvironmentOutlined className="feishu-muted-icon" /> {display(row.fields?.["工作地点"])}</Text> },
    { title: "招聘类型", width: 130, render: (_, row) => row.fields?.["招聘类型"] ? <Tag color="purple">{row.fields["招聘类型"]}</Tag> : "—" },
    { title: "学历", width: 100, render: (_, row) => display(row.fields?.["学历"]) },
    { title: "网申截止", width: 145, render: (_, row) => <Text>{display(row.fields?.["网申截止"])}</Text> },
    { title: "更新日期", width: 150, render: (_, row) => <Text type="secondary">{display(row.fields?.["网申更新"])}</Text> },
    { title: "操作", width: 90, fixed: "right", render: (_, row) => <Button size="small" onClick={() => open(row)}>详情 <ArrowRightOutlined /></Button> },
  ];
  const last = manifest?.status?.last_run;
  const activeFilters = Object.values(filters).filter(Boolean).length + Number(!!status) + Number(priority >= 0) + Number(!!tableId) + Number(!!search);

  return <div className="feishu-workspace">
    <section className="feishu-hero">
      <div>
        <div className="feishu-eyebrow"><BookOutlined /> 个人求职工作台</div>
        <Title level={1}>飞书职位库</Title>
        <Paragraph>把海量招聘信息整理成你的投递清单。搜索、筛选、标记进度，都在这里完成。</Paragraph>
        <Space wrap className="feishu-hero-meta">
          <span><BankOutlined /> {manifest?.status?.records?.toLocaleString() || "—"} 条职位</span>
          <span><ClockCircleOutlined /> {last?.finished ? `更新于 ${dateText(last.finished)}` : "等待首次同步"}</span>
          <span><CheckCircleOutlined /> 仅管理员可见</span>
        </Space>
      </div>
      <div className="feishu-hero-action">
        <Button icon={<ReloadOutlined />} onClick={() => { loadManifest(); setRevision((value) => value + 1); }}>刷新</Button>
        <Button icon={<SyncOutlined />} onClick={sync} loading={syncing || manifest?.status?.running} disabled={!manifest?.status?.configured}>同步源表</Button>
      </div>
    </section>

    {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => { loadManifest(); setRevision((value) => value + 1); }}>重试</Button>} />}
    {last?.status === "error" && <Alert type="warning" showIcon message="上次同步失败，当前仍可使用已保存的数据" description={last.message} />}
    {manifest?.status && !manifest.status.configured && <Alert type="warning" showIcon message="飞书公开视图尚未配置" />}

    <Row gutter={[14, 14]} className="feishu-summary">
      <Col xs={12} md={6}><Card><span>当前结果</span><strong>{total.toLocaleString()}</strong><small>条符合筛选条件</small></Card></Col>
      <Col xs={12} md={6}><Card><span>关注中</span><strong>{statusCounts["关注"] || 0}</strong><small>值得优先跟进</small></Card></Col>
      <Col xs={12} md={6}><Card><span>已投递</span><strong>{statusCounts["已投递"] || 0}</strong><small>简历已发出</small></Card></Col>
      <Col xs={12} md={6}><Card><span>面试中</span><strong>{statusCounts["面试"] || 0}</strong><small>正在推进</small></Card></Col>
    </Row>

    <Card className="feishu-filter-card" bordered={false}>
      <div className="feishu-search-row">
        <Input size="large" prefix={<SearchOutlined />} placeholder="搜索公司、岗位、技术方向、地点或任意原始字段" allowClear value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索飞书职位" />
        <Button icon={<FilterOutlined />} onClick={() => setMoreFilters((value) => !value)}>{moreFilters ? "收起筛选" : "更多筛选"}{activeFilters ? ` · ${activeFilters}` : ""}</Button>
      </div>
      <div className="feishu-status-row">
        <Button type="text" className={!status ? "active" : ""} onClick={() => { setStatus(""); setPage(1); }}>全部 <b>{manifest?.status?.records || 0}</b></Button>
        {STATUSES.map((value) => <Button type="text" key={value} className={value === status ? "active" : ""} onClick={() => { setStatus(value); setPage(1); }}>
          <Tag color={STATUS_COLORS[value]}>{value}</Tag><b>{statusCounts[value] || 0}</b>
        </Button>)}
      </div>
      {moreFilters && <div className="feishu-extra-filters">
        {FIELDS.map(([key, label, facet]) => <Select key={key} placeholder={label} aria-label={label} showSearch allowClear optionFilterProp="label" value={filters[key] || undefined} onChange={(value) => updateFilter(key, value)} options={options(facet)} />)}
        <Select placeholder="优先级" aria-label="优先级" allowClear value={priority < 0 ? undefined : priority} onChange={(value) => { setPriority(value ?? -1); setPage(1); }} options={[0, 1, 2, 3].map((value) => ({ value, label: value ? `${"★".repeat(value)} · ${value} 星` : "未标重点" }))} />
        {manifest?.tables?.length > 1 && <Select placeholder="数据表" allowClear value={tableId || undefined} onChange={(value) => { setTableId(value || ""); setPage(1); }} options={manifest.tables.map((item) => ({ value: item.table_id, label: `${item.name} · ${item.records}` }))} />}
        <Button type="link" onClick={reset}>清除全部筛选</Button>
      </div>}
    </Card>

    <Card className="feishu-results-card" bordered={false}>
      <div className="feishu-results-toolbar">
        <div><Title level={4}>职位清单</Title><Text type="secondary">共 {total.toLocaleString()} 条，点击公司查看详情与跟进记录</Text></div>
        <Space wrap>
          <Select aria-label="分组方式" value={groupBy || undefined} allowClear placeholder="不分组" style={{ width: 150 }} onChange={(value) => { setGroupBy(value || ""); setPage(1); }} options={GROUPS.map(([value, label]) => ({ value, label: `按${label}分组` }))} />
          <Select aria-label="排序字段" value={sort} style={{ width: 145 }} onChange={(value) => { setSort(value); setPage(1); }} options={SORTS.map(([value, label]) => ({ value, label }))} />
          <Tooltip title={direction === "asc" ? "当前升序，点击改为降序" : "当前降序，点击改为升序"}><Button aria-label="切换排序方向" icon={direction === "asc" ? <SortAscendingOutlined /> : <SortDescendingOutlined />} onClick={() => { setDirection((value) => value === "asc" ? "desc" : "asc"); setPage(1); }} /></Tooltip>
          {activeFilters > 0 && <Button type="text" onClick={reset}>清除筛选</Button>}
        </Space>
      </div>
      <Table className="feishu-table" rowKey={(row) => `${row.table_id}/${row.record_id}`} dataSource={rows} columns={columns}
        loading={loading} size="middle" tableLayout="fixed" scroll={{ x: groupBy ? 1630 : 1490 }}
        locale={{ emptyText: <Empty description="没有找到符合条件的职位" /> }}
        pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: setPage, showTotal: (count, range) => `${range[0]}–${range[1]} / ${count.toLocaleString()}` }} />
    </Card>

    <Drawer className="feishu-detail-drawer" open={!!selected} onClose={() => { setSelected(null); setDraft(null); }} width={780}
      title={selected ? <div className="feishu-drawer-heading"><Text type="secondary">职位详情 / 跟进</Text><Title level={3}>{selected.fields?.["公司名称"] || selected.fields?.["企业名称"] || "未填写公司"}</Title></div> : "职位详情"}>
      {selected && draft && <>
        <Space wrap className="feishu-detail-tags">
          <Tag color={STATUS_COLORS[draft.status]}>{draft.status}</Tag>
          {selected.fields?.["公司行业"] && <Tag color="cyan">{selected.fields["公司行业"]}</Tag>}
          {selected.fields?.["招聘类型"] && <Tag color="purple">{selected.fields["招聘类型"]}</Tag>}
          {selected.fields?.["工作地点"] && <Tag icon={<EnvironmentOutlined />}>{selected.fields["工作地点"]}</Tag>}
        </Space>
        <Title level={5}>招聘岗位</Title>
        <Paragraph className="feishu-position-detail">{display(selected.fields?.["校招岗位"] || selected.fields?.["招聘岗位"] || selected.fields?.["岗位"])}</Paragraph>
        <Space wrap className="feishu-apply-links">
          {[["网申公告", selected.fields?.["网申公告"]], ["投递链接", selected.fields?.["投递链接"]]].map(([label, value]) => safeHref(value) ? <Button key={label} href={safeHref(value)} target="_blank" rel="noopener noreferrer" icon={<ExportOutlined />}>{label}</Button> : null)}
        </Space>
        <Card className="feishu-follow-card" title={<Space><EditOutlined /> 我的跟进</Space>}>
          <div className="feishu-follow-fields">
            <div><label>进度</label><Select value={draft.status} onChange={(value) => setDraft({ ...draft, status: value })} options={STATUSES.map((value) => ({ value, label: value }))} /></div>
            <div><label>优先级</label><Select value={draft.priority} onChange={(value) => setDraft({ ...draft, priority: value })} options={[0, 1, 2, 3].map((value) => ({ value, label: value ? `${"★".repeat(value)} · ${value} 星` : "未标重点" }))} /></div>
            <div className="wide"><label>自定义标签</label><Input placeholder="例如：内推、有笔试、薪资合适，用逗号分隔" value={draft.tags} onChange={(event) => setDraft({ ...draft, tags: event.target.value })} /></div>
            <div className="wide"><label>备注</label><Input.TextArea rows={4} placeholder="记录投递渠道、联系人、薪资或下一步" value={draft.note} onChange={(event) => setDraft({ ...draft, note: event.target.value })} /></div>
          </div>
          <Button type="primary" loading={saving} onClick={() => save()} icon={<CheckCircleOutlined />}>保存跟进</Button>
        </Card>
        <div className="feishu-raw-heading"><Title level={5}>完整资料</Title><Text type="secondary">来自飞书原始表格</Text></div>
        <Descriptions bordered size="small" column={1} items={Object.entries(selected.fields || {}).map(([key, value]) => ({ key, label: key, children: safeHref(value) ? <a href={safeHref(value)} target="_blank" rel="noopener noreferrer">打开链接 <ExportOutlined /></a> : <span className="feishu-field-value">{display(value)}</span> }))} />
      </>}
    </Drawer>
  </div>;
}
