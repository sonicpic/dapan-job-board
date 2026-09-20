import React, { useState, useEffect, useMemo } from "react";
import { createRoot } from "react-dom/client";
import {
  App as AntApp,
  ConfigProvider,
  Layout,
  Button,
  Space,
  Typography,
  Tag,
  Card,
  Input,
  Select,
  Segmented,
  Row,
  Col,
  List,
  Pagination,
  Drawer,
  Descriptions,
  Alert,
  Empty,
  Skeleton,
  Tooltip,
  Avatar,
  Badge,
  Calendar,
  Divider,
  Form,
  Switch,
  Table,
  Tabs,
  Modal,
  Popconfirm,
  Result,
  Statistic,
  Progress,
  Grid,
  Checkbox,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  CompassOutlined,
  ArrowRightOutlined,
  ArrowLeftOutlined,
  SearchOutlined,
  ExportOutlined,
  EnvironmentOutlined,
  CalendarOutlined,
  BankOutlined,
  BookOutlined,
  ClockCircleOutlined,
  SyncOutlined,
  CheckCircleOutlined,
  StarOutlined,
  StarFilled,
  AppstoreOutlined,
  UnorderedListOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  PushpinOutlined,
  PlusOutlined,
  EditOutlined,
  ReloadOutlined,
  LogoutOutlined,
  MailOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  InfoCircleOutlined,
  RadarChartOutlined,
  LinkOutlined,
  BellOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import "dayjs/locale/zh-cn";
import "antd/dist/reset.css";
import "./style.css";

dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.locale("zh-cn");
const { Title, Text, Paragraph, Link } = Typography;
const theme = {
  token: {
    colorPrimary: "#176456",
    colorInfo: "#176456",
    colorSuccess: "#237f68",
    colorText: "#23362f",
    colorTextSecondary: "#7a847e",
    colorBgLayout: "#f6f7f4",
    colorBorderSecondary: "#e9ede8",
    borderRadius: 10,
    fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: 14,
    controlHeight: 38,
  },
  components: {
    Button: { primaryShadow: "none" },
    Card: { paddingLG: 22 },
    Tabs: { horizontalItemGutter: 30 },
    Table: { headerBg: "#f5f7f4" },
    Input: { activeShadow: "0 0 0 2px rgba(23,100,86,.08)" },
  },
};
const SOURCE = "https://www.kdocs.cn/";
const fmt = (v, pattern = "MM-DD HH:mm") =>
  v ? dayjs(v).tz("Asia/Shanghai").format(pattern) : "尚未同步";
const nowCN = () => dayjs().tz("Asia/Shanghai");
const statusInfo = {
  open: ["报名中", "green"],
  unknown: ["截止时间未注明", "default"],
  expired: ["已截止", "default"],
  upcoming: ["即将开始", "blue"],
  today: ["今天", "green"],
  started: ["已开始", "orange"],
  ended: ["已结束", "default"],
};
const statusTag = (r) => {
  let [label, color] = statusInfo[r.status] || ["时间待确认", "default"];
  if (r.kind === "event" && r.status === "unknown") label = "时间待确认";
  if (r.kind === "job" && r.status === "upcoming") label = "尚未开始";
  return (
    <Tag color={color} bordered={false}>
      {label}
    </Tag>
  );
};
const reviewStyle = (review) => {
  const score = review?.score;
  if (score >= 75) return { tier: "high", color: "green", stroke: "#3f8f66" };
  if (score >= 60) return { tier: "medium", color: "cyan", stroke: "#278c8b" };
  if (score >= 40) return { tier: "caution", color: "orange", stroke: "#d28b32" };
  return { tier: "low", color: "red", stroke: "#c35c55" };
};
function ReviewTag({ review, compact = false }) {
  if (review?.score === null || review?.score === undefined) return null;
  const style = reviewStyle(review);
  return (
    <Tag color={style.color} bordered={false} icon={<RadarChartOutlined />}>
      {compact ? `${review.score} · ${review.label}` : `网评参考 ${review.score} · ${review.label}`}
    </Tag>
  );
}
const validUrl = (u) =>
  typeof u === "string" && /^(https?:\/\/|mailto:)/i.test(u) ? u : undefined;
async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "job-board",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try {
    data = await r.json();
  } catch {
    data = {
      detail:
        r.status === 429
          ? "请求过于频繁，请稍后重试"
          : r.status === 403
            ? "当前访问地址未被服务端允许"
            : "服务暂时不可用，请稍后重试",
    };
  }
  if (!r.ok) {
    const error = new Error(
      typeof data.detail === "string" ? data.detail : "请检查表单内容",
    );
    error.status = r.status;
    throw error;
  }
  return data;
}
function sourceButton(source) {
  return (
    <Button
      href={source || SOURCE}
      target="_blank"
      rel="noopener noreferrer"
      icon={<ExportOutlined />}
    >
      查看原表
    </Button>
  );
}
function Header({ active = "jobs", onChange, config, admin = false }) {
  return (
    <header className="header">
      <div className="header-inner">
        <a className="brand" href="/" aria-label="大潘的就业情报站首页">
          <span className="brand-mark">
            <CompassOutlined />
          </span>
          <span>{config?.title || "大潘的就业情报站"}</span>
        </a>
        {!admin && (
          <Segmented
            className="nav-tabs"
            value={active}
            onChange={onChange}
            options={[
              { label: "招聘信息", value: "jobs", icon: <BankOutlined /> },
              {
                label: "宣讲日程",
                value: "events",
                icon: <CalendarOutlined />,
              },
              { label: "我的收藏", value: "saved", icon: <StarOutlined /> },
            ]}
          />
        )}
        <Space className="header-actions">
          {sourceButton(config?.source_url)}
          <Tooltip title={admin ? "返回招聘信息" : "管理后台"}>
            <Button
              type="text"
              href={admin ? "/" : "/admin"}
              icon={admin ? <ArrowLeftOutlined /> : <SettingOutlined />}
              aria-label={admin ? "返回首页" : "管理后台"}
            />
          </Tooltip>
        </Space>
      </div>
    </header>
  );
}
function Detail({ record, onClose, source, saved, toggle }) {
  if (!record) return null;
  const event = record.kind === "event";
  const rows = event
    ? [
        ["宣讲时间", record.time_text],
        ["地点", record.location],
        ["补充信息", record.notes],
      ]
    : [
        ["招聘岗位", record.positions],
        ["工作地点", record.location],
        ["学历要求", record.education],
        ["单位类型", record.category],
        ["薪资情况", record.salary],
        ["开始时间", record.start_date || record.start],
        ["截止时间", record.deadline_date || record.deadline],
        ["投递方式", record.method],
        ["投递说明", record.application],
        ["补充信息 / 内推码", record.notes],
      ];
  return (
    <Drawer
      open
      title={event ? "宣讲会详情" : "招聘详情"}
      onClose={onClose}
      width={620}
      extra={
        <Button
          icon={saved ? <StarFilled /> : <StarOutlined />}
          onClick={() => toggle(record.id)}
        >
          {saved ? "已收藏" : "收藏"}
        </Button>
      }
    >
      <Space wrap>
        {statusTag(record)}
        {record.pinned && (
          <Tag color="gold" icon={<PushpinOutlined />}>
            置顶
          </Tag>
        )}
        {record.modified && <Tag>管理员已补充</Tag>}
      </Space>
      <Title level={3}>{record.company}</Title>
      <Descriptions
        column={1}
        layout="vertical"
        items={rows.map(([label, value]) => ({
          key: label,
          label,
          children: <span className="preserve">{value || "原表未注明"}</span>,
        }))}
      />
      {record.review && (
        <>
          <Divider />
          <section className="review-detail">
            <div className="review-heading">
              <div>
                <Text type="secondary">公开网评分析</Text>
                <Title level={4}>推荐程度参考</Title>
              </div>
              <ReviewTag review={record.review} />
            </div>
            <Progress
              percent={record.review.score}
              strokeColor={reviewStyle(record.review).stroke}
              trailColor="#edf0ec"
              format={(value) => `${value} 分`}
            />
            <Paragraph className="review-summary">{record.review.summary}</Paragraph>
            <div className="review-points">
              <Card size="small" title="常见正面反馈">
                {record.review.pros?.length ? (
                  <List
                    size="small"
                    split={false}
                    dataSource={record.review.pros}
                    renderItem={(item) => <List.Item>{item}</List.Item>}
                  />
                ) : (
                  <Text type="secondary">公开材料中没有形成明确共识</Text>
                )}
              </Card>
              <Card size="small" title="常见顾虑">
                {record.review.cons?.length ? (
                  <List
                    size="small"
                    split={false}
                    dataSource={record.review.cons}
                    renderItem={(item) => <List.Item>{item}</List.Item>}
                  />
                ) : (
                  <Text type="secondary">公开材料中没有形成明确共识</Text>
                )}
              </Card>
            </div>
            <Title level={5}>参考来源</Title>
            <List
              className="review-sources"
              size="small"
              dataSource={record.review.sources || []}
              renderItem={(item) => (
                <List.Item>
                  <Link href={validUrl(item.url)} target="_blank" rel="noopener noreferrer">
                    <LinkOutlined /> {item.title || item.url}
                  </Link>
                </List.Item>
              )}
            />
            <Alert
              type="info"
              showIcon
              message="网评参考基于公开网页整理，不代表事实定论。信息可能存在样本偏差或已经过时，请结合官方信息和个人情况判断。"
              description={`分析时间：${fmt(record.review.updated_at, "YYYY-MM-DD HH:mm")} · 共 ${record.review.sources?.length || 0} 个参考来源`}
            />
          </section>
        </>
      )}
      <Divider />
      <Space wrap>
        {validUrl(record.apply_url) && (
          <Button
            type="primary"
            size="large"
            href={record.apply_url}
            target="_blank"
            rel="noopener noreferrer"
            icon={
              record.apply_url.startsWith("mailto:") ? (
                <MailOutlined />
              ) : (
                <ExportOutlined />
              )
            }
          >
            {event
              ? "查看报名入口"
              : record.apply_url.startsWith("mailto:")
                ? "邮件投递"
                : "前往投递"}
          </Button>
        )}
        {validUrl(record.announcement_url) && (
          <Button
            size="large"
            href={record.announcement_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            招聘公告
          </Button>
        )}
        {sourceButton(source)}
      </Space>
      <Paragraph type="secondary" className="detail-note">
        {record.source === "manual"
          ? "由管理员补充"
          : `来源：${record.source_sheet} · 第 ${record.source_row} 行`}
        <br />
        原表更新时间：{record.updated || "未注明"}
        <br />
        岗位要求、投递有效性及宣讲会安排请以官方信息为准。
      </Paragraph>
    </Drawer>
  );
}
function JobCard({ record: r, onOpen, saved, toggle }) {
  const review = r.review ? reviewStyle(r.review) : null;
  return (
    <Card
      className={
        "job-card" +
        (r.status === "expired" ? " muted-card" : "") +
        (review ? ` review-tier-${review.tier}` : "")
      }
    >
      <div className="job-top">
        <Avatar shape="square" size={44} className="company-avatar">
          {r.company.slice(0, 2)}
        </Avatar>
        <div className="company-title">
          <Button
            type="link"
            className="company-link"
            onClick={() => onOpen(r)}
          >
            {r.company}
          </Button>
          <Text type="secondary">{r.category || "单位类型未注明"}</Text>
        </div>
        <Button
          className="save-button"
          type="text"
          aria-label={saved ? "取消收藏 " + r.company : "收藏 " + r.company}
          icon={
            saved ? (
              <StarFilled style={{ color: "#ba913b" }} />
            ) : (
              <StarOutlined />
            )
          }
          onClick={() => toggle(r.id)}
        />
      </div>
      <Paragraph className="positions" ellipsis={{ rows: 2, tooltip: false }}>
        {r.positions || "岗位详情请查看招聘公告"}
      </Paragraph>
      <div className="job-meta">
        <Text>
          <EnvironmentOutlined /> {r.location || "地点未注明"}
        </Text>
        <Text>
          <BookOutlined /> {r.education || "学历未注明"}
        </Text>
      </div>
      {r.salary && <Text className="salary">{r.salary}</Text>}
      <div className="job-bottom">
        <Space size={3} wrap>
          {r.pinned && (
            <Tag color="gold" bordered={false}>
              <PushpinOutlined /> 置顶
            </Tag>
          )}
          {statusTag(r)}
          <ReviewTag review={r.review} compact />
        </Space>
        <Button type="text" onClick={() => onOpen(r)} className="detail-button">
          查看详情 <ArrowRightOutlined />
        </Button>
      </div>
    </Card>
  );
}
function EventList({ items, onOpen, saved, toggle }) {
  const groups = Object.groupBy
    ? Object.groupBy(items, (r) => r.date || "时间待定")
    : items.reduce((a, r) => ((a[r.date || "时间待定"] ||= []).push(r), a), {});
  return (
    <div className="event-groups">
      {Object.entries(groups)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, records]) => (
          <section key={date}>
            <div className="day-heading">
              <Title level={4}>
                {date === "时间待定" ? date : dayjs(date).format("M月D日")}
              </Title>
              <Text type="secondary">
                {date === "时间待定"
                  ? "请查看原表"
                  : dayjs(date).format("dddd")}
                {date === nowCN().format("YYYY-MM-DD") ? " · 今天" : ""}
              </Text>
              <Tag bordered={false}>{records.length} 场</Tag>
            </div>
            <List
              dataSource={records}
              renderItem={(r) => (
                <List.Item className="event-row" key={r.id}>
                  <div className="event-time">
                    <Text strong>
                      {r.time_known ? fmt(r.starts_at, "HH:mm") : "待定"}
                    </Text>
                    {statusTag(r)}
                  </div>
                  <div className="event-content">
                    <Button
                      type="link"
                      onClick={() => onOpen(r)}
                      className="event-company"
                    >
                      {r.company}
                    </Button>
                    <ReviewTag review={r.review} compact />
                    <Text type="secondary">
                      <EnvironmentOutlined /> {r.location || "地点待定"}
                    </Text>
                    {r.notes && (
                      <Paragraph ellipsis={{ rows: 1 }} className="event-note">
                        {r.notes}
                      </Paragraph>
                    )}
                  </div>
                  <Button
                    type="text"
                    aria-label={
                      saved.includes(r.id)
                        ? "取消收藏 " + r.company
                        : "收藏 " + r.company
                    }
                    icon={
                      saved.includes(r.id) ? <StarFilled /> : <StarOutlined />
                    }
                    onClick={() => toggle(r.id)}
                  />
                  <Button className="event-detail" onClick={() => onOpen(r)}>
                    详情 <ArrowRightOutlined />
                  </Button>
                </List.Item>
              )}
            />
          </section>
        ))}
    </div>
  );
}

function PublicPage() {
  const { message } = AntApp.useApp();
  const screens = Grid.useBreakpoint();
  const [data, setData] = useState(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [tab, setTab] = useState("jobs"),
    [query, setQuery] = useState(""),
    [city, setCity] = useState(),
    [education, setEducation] = useState(),
    [category, setCategory] = useState(),
    [jobStatus, setJobStatus] = useState("active"),
    [eventStatus, setEventStatus] = useState("upcoming"),
    [mode, setMode] = useState("cards"),
    [page, setPage] = useState(1),
    [selected, setSelected] = useState(null),
    [date, setDate] = useState(null),
    [calendar, setCalendar] = useState(false),
    [saved, setSaved] = useState(() => {
      try {
        return JSON.parse(localStorage.getItem("job-bookmarks") || "[]");
      } catch {
        return [];
      }
    });
  const load = async () => {
    try {
      setData(await api("/public"));
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 900000);
    const onFocus = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, []);
  useEffect(() => {
    setPage(1);
  }, [query, city, education, category, jobStatus, eventStatus, tab, date]);
  useEffect(() => {
    if (data) document.title = data.config.title + " · 校园招聘与宣讲会";
  }, [data?.config.title]);
  const toggle = (id) =>
    setSaved((old) => {
      const next = old.includes(id)
        ? old.filter((x) => x !== id)
        : [...old, id];
      try {
        localStorage.setItem("job-bookmarks", JSON.stringify(next));
      } catch {
        message.warning("浏览器未允许保存收藏，本次浏览仍可使用");
      }
      return next;
    });
  const all = data?.records || [],
    jobs = all.filter((r) => r.kind === "job"),
    events = all.filter((r) => r.kind === "event"),
    today = nowCN().format("YYYY-MM-DD");
  const dayEvents = events
    .filter((r) => r.date === today && r.status !== "ended")
    .sort((a, b) => (a.starts_at || "").localeCompare(b.starts_at || ""));
  const cities = [
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "南京",
    "成都",
    "武汉",
    "西安",
    "长沙",
    "苏州",
    "天津",
    "厦门",
    "合肥",
    "珠海",
    "宁波",
  ].filter((c) => jobs.some((r) => r.location?.includes(c)));
  const matches = (r) =>
    !query.trim() ||
    [
      r.company,
      r.positions,
      r.location,
      r.education,
      r.notes,
      r.time_text,
    ].some((x) => x?.toLowerCase().includes(query.trim().toLowerCase()));
  const filtered = useMemo(() => {
    let records =
      tab === "events"
        ? events
        : tab === "saved"
          ? all.filter((r) => saved.includes(r.id))
          : jobs;
    records = records.filter(matches);
    if (tab === "jobs")
      records = records.filter(
        (r) =>
          (!city || r.location?.includes(city)) &&
          (!education || r.education?.includes(education)) &&
          (!category || r.category === category) &&
          (jobStatus === "all" ||
            (jobStatus === "active" && r.status !== "expired") ||
            r.status === jobStatus),
      );
    if (tab === "events")
      records = records.filter(
        (r) =>
          (!date || r.date === date) &&
          (eventStatus === "all" ||
            (eventStatus === "today" && r.date === today) ||
            (eventStatus === "upcoming" && r.status !== "ended") ||
            (eventStatus === "ended" && r.status === "ended")),
      );
    return records.sort(
      (a, b) =>
        Number(b.pinned) - Number(a.pinned) ||
        (a.kind === "event" && b.kind === "event"
          ? (a.starts_at || "9999").localeCompare(b.starts_at || "9999")
          : (b.updated_date || "").localeCompare(a.updated_date || "")) ||
        (b.source_row || 0) - (a.source_row || 0),
    );
  }, [
    data,
    tab,
    query,
    city,
    education,
    category,
    jobStatus,
    eventStatus,
    date,
    saved,
  ]);
  const changeTab = (t) => {
    setTab(t);
    setQuery("");
    setDate(null);
  };
  const reset = () => {
    setQuery("");
    setCity();
    setEducation();
    setCategory();
    setJobStatus("active");
    setEventStatus("upcoming");
    setDate(null);
  };
  const configs = data?.config;
  return (
    <>
      <Header active={tab} onChange={changeTab} config={configs} />
      <main className="page">
        {error && (
          <Alert
            type="error"
            showIcon
            message="暂时无法更新信息"
            description={error}
            action={<Button onClick={load}>重试</Button>}
            closable
          />
        )}
        {data?.sync.has_error && (
          <Alert
            type="warning"
            showIcon
            message="原表同步暂时失败，当前展示上一次成功同步的数据。"
          />
        )}
        <div className="content-grid">
          <div className="main-column">
            <Card className="filter-card">
              <div className="section-top">
                <Space size={10}>
                  <Title level={3}>
                    {tab === "events"
                      ? "宣讲日程"
                      : tab === "saved"
                        ? "我的收藏"
                        : "招聘信息"}
                  </Title>
                  <Tag bordered={false}>{filtered.length}</Tag>
                </Space>
                <Space>
                  {tab === "events" ? (
                    <Button
                      icon={<CalendarOutlined />}
                      type={calendar ? "primary" : "default"}
                      onClick={() => setCalendar(!calendar)}
                    >
                      日历
                    </Button>
                  ) : (
                    <Segmented
                      size="small"
                      aria-label="列表显示方式"
                      options={[
                        {
                          value: "cards",
                          icon: <AppstoreOutlined />,
                          label: "卡片",
                        },
                        {
                          value: "table",
                          icon: <UnorderedListOutlined />,
                          label: "列表",
                        },
                      ]}
                      value={mode}
                      onChange={setMode}
                    />
                  )}
                </Space>
              </div>
              <Input
                size="large"
                prefix={<SearchOutlined />}
                placeholder={
                  tab === "events"
                    ? "搜索宣讲企业、地点或活动名称"
                    : "搜索公司、岗位、技术方向…"
                }
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                allowClear
                aria-label="搜索信息"
              />
              <div className="filter-row">
                {tab === "jobs" ? (
                  <>
                    <Select
                      aria-label="工作城市"
                      placeholder="全部城市"
                      value={city}
                      onChange={setCity}
                      allowClear
                      options={cities.map((value) => ({ value, label: value }))}
                    />
                    <Select
                      aria-label="学历要求"
                      placeholder="全部学历"
                      value={education}
                      onChange={setEducation}
                      allowClear
                      options={["本科", "硕", "博"].map((value) => ({
                        value,
                        label: { 本科: "本科", 硕: "硕士", 博: "博士" }[value],
                      }))}
                    />
                    <Select
                      aria-label="单位类型"
                      placeholder="全部类型"
                      value={category}
                      onChange={setCategory}
                      allowClear
                      options={[
                        ...new Set(jobs.map((r) => r.category).filter(Boolean)),
                      ].map((value) => ({ value, label: value }))}
                    />
                    <Select
                      aria-label="招聘状态"
                      value={jobStatus}
                      onChange={setJobStatus}
                      options={[
                        { value: "active", label: "未标记截止" },
                        { value: "all", label: "全部状态" },
                        { value: "open", label: "报名中" },
                        { value: "expired", label: "已截止" },
                      ]}
                    />
                  </>
                ) : tab === "events" ? (
                  <Segmented
                    value={eventStatus}
                    onChange={setEventStatus}
                    options={[
                      { value: "upcoming", label: "待参加" },
                      { value: "today", label: "今天" },
                      { value: "all", label: "全部" },
                      { value: "ended", label: "往期" },
                    ]}
                  />
                ) : (
                  <Text type="secondary">
                    {saved.length} 条收藏 · 仅在本机保存
                  </Text>
                )}
                <Button type="text" onClick={reset}>
                  重置
                </Button>
                {date && (
                  <Tag closable onClose={() => setDate(null)}>
                    {date}
                  </Tag>
                )}
              </div>
              {calendar && tab === "events" && (
                <div className="calendar-wrapper">
                  <Calendar
                    fullscreen={false}
                    value={date ? dayjs(date) : nowCN()}
                    onSelect={(v, info) => {
                      if (info.source === "date")
                        setDate(v.format("YYYY-MM-DD"));
                    }}
                    cellRender={(value, info) =>
                      info.type === "date" &&
                      events.some(
                        (r) => r.date === value.format("YYYY-MM-DD"),
                      ) ? (
                        <Badge color="#176456" />
                      ) : null
                    }
                  />
                </div>
              )}
            </Card>
            {loading ? (
              <Card>
                <Skeleton active paragraph={{ rows: 8 }} />
              </Card>
            ) : !filtered.length ? (
              <Card>
                <Empty
                  description={
                    tab === "saved"
                      ? "还没有收藏，点击信息卡片上的星标试试"
                      : data?.sync.last_success
                        ? "没有找到符合条件的信息"
                        : "正在进行首次同步，请稍后刷新"
                  }
                >
                  <Button onClick={reset}>清除筛选</Button>
                  <Button type="link" onClick={load}>
                    刷新数据
                  </Button>
                </Empty>
              </Card>
            ) : tab === "events" ? (
              <EventList
                items={filtered}
                onOpen={setSelected}
                saved={saved}
                toggle={toggle}
              />
            ) : mode === "table" ? (
              <Card className="table-card">
                <Table
                  rowKey="id"
                  dataSource={filtered}
                  tableLayout="fixed"
                  scroll={{ x: screens.md ? 820 : 245 }}
                  pagination={{
                    pageSize: 12,
                    showSizeChanger: false,
                    current: page,
                    onChange: setPage,
                  }}
                  columns={[
                    {
                      title: "单位 / 岗位",
                      key: "company",
                      width: screens.md ? 340 : 155,
                      render: (_, r) => (
                        <>
                          <Button
                            type="link"
                            className="table-company"
                            onClick={() => setSelected(r)}
                          >
                            {r.company}
                          </Button>
                          <ReviewTag review={r.review} compact />
                          <Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                            {r.positions || r.time_text || "详见公告"}
                          </Paragraph>
                        </>
                      ),
                    },
                    {
                      title: "地点",
                      dataIndex: "location",
                      width: 270,
                      responsive: ["md"],
                      render: (v) => (
                        <Paragraph
                          className="table-location"
                          ellipsis={{ rows: 3, tooltip: v || "未注明" }}
                        >
                          {v || "未注明"}
                        </Paragraph>
                      ),
                    },
                    {
                      title: "状态",
                      key: "status",
                      width: screens.md ? 100 : 90,
                      render: (_, r) => statusTag(r),
                    },
                    {
                      title: "操作",
                      key: "action",
                      width: screens.md ? 70 : 60,
                      responsive: ["md"],
                      render: (_, r) => (
                        <Button
                          className="table-action"
                          size="small"
                          onClick={() => setSelected(r)}
                        >
                          详情
                        </Button>
                      ),
                    },
                  ]}
                />
              </Card>
            ) : (
              <>
                <List
                  grid={{
                    gutter: 16,
                    xs: 1,
                    sm: 1,
                    md: 2,
                    lg: 2,
                    xl: 2,
                    xxl: 2,
                  }}
                  dataSource={filtered.slice((page - 1) * 12, page * 12)}
                  renderItem={(r) => (
                    <List.Item key={r.id}>
                      {r.kind === "job" ? (
                        <JobCard
                          record={r}
                          onOpen={setSelected}
                          saved={saved.includes(r.id)}
                          toggle={toggle}
                        />
                      ) : (
                        <Card>
                          <Space>
                            {statusTag(r)}
                            <ReviewTag review={r.review} compact />
                            <Text>{r.time_text}</Text>
                          </Space>
                          <Title level={4}>{r.company}</Title>
                          <Paragraph type="secondary">{r.location}</Paragraph>
                          <Button onClick={() => setSelected(r)}>
                            查看宣讲会 <ArrowRightOutlined />
                          </Button>
                          <Button
                            type="text"
                            icon={<StarFilled />}
                            aria-label={"取消收藏 " + r.company}
                            onClick={() => toggle(r.id)}
                          />
                        </Card>
                      )}
                    </List.Item>
                  )}
                />
                <Pagination
                  align="center"
                  current={page}
                  onChange={setPage}
                  total={filtered.length}
                  pageSize={12}
                  showSizeChanger={false}
                  hideOnSinglePage
                />
              </>
            )}
          </div>
          <aside className="sidebar">
            <Card
              className="today-card"
              title={
                <Space>
                  <CalendarOutlined />
                  <span>今天的宣讲会</span>
                </Space>
              }
              extra={
                <Tag color="green" bordered={false}>
                  {dayEvents.length} 场
                </Tag>
              }
            >
              {dayEvents.length ? (
                <List
                  dataSource={dayEvents.slice(0, 5)}
                  renderItem={(r) => (
                    <List.Item>
                      <Button
                        type="text"
                        className="aside-event"
                        onClick={() => setSelected(r)}
                      >
                        <span className="aside-time">
                          {r.time_known
                            ? fmt(r.starts_at, "HH:mm")
                            : "时间待定"}
                          {r.status === "started" && <small>已开始</small>}
                        </span>
                        <span className="aside-name">
                          {r.company}
                          <small>
                            <EnvironmentOutlined /> {r.location}
                          </small>
                        </span>
                      </Button>
                    </List.Item>
                  )}
                />
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="今天暂无宣讲会"
                />
              )}
              <Button
                block
                onClick={() => {
                  changeTab("events");
                  setEventStatus("today");
                }}
              >
                查看今日日程 <ArrowRightOutlined />
              </Button>
            </Card>
            <Card className="source-card">
              <span className="sidebar-label">数据与更新</span>
              <Title level={5}>
                <CheckCircleOutlined style={{ color: "#176456" }} />{" "}
                来自学院共享表格
              </Title>
              <Paragraph type="secondary">
                {data?.sync.source_title || "软件学院就业岗位推荐信息汇总"}
              </Paragraph>
              <Divider />
              <div className="sync-row">
                <Text type="secondary">最近同步</Text>
                <Text>{fmt(data?.sync.last_success)}</Text>
              </div>
              <div className="sync-row">
                <Text type="secondary">下次检查</Text>
                <Text>
                  {configs?.auto_sync
                    ? fmt(data?.sync.next_sync, "HH:mm")
                    : "已暂停"}
                </Text>
              </div>
              <Button
                block
                icon={<ReloadOutlined />}
                onClick={async () => {
                  await load();
                  message.info("已刷新当前数据");
                }}
              >
                刷新当前页面
              </Button>
            </Card>
          </aside>
        </div>
      </main>
      <Detail
        record={selected}
        onClose={() => setSelected(null)}
        source={configs?.source_url}
        saved={saved.includes(selected?.id)}
        toggle={toggle}
      />
    </>
  );
}

function AdminPage() {
  const { message } = AntApp.useApp();
  const screens = Grid.useBreakpoint();
  const [data, setData] = useState(null),
    [loading, setLoading] = useState(true),
    [unauthorized, setUnauthorized] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [kind, setKind] = useState("job"),
    [query, setQuery] = useState(""),
    [reviewQuery, setReviewQuery] = useState(""),
    [reviewStatus, setReviewStatus] = useState("all"),
    [edit, setEdit] = useState(null),
    [editInitial, setEditInitial] = useState({});
  const savedAdminLogin = useMemo(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("job-admin-login") || "null");
      return saved?.username && saved?.password ? saved : null;
    } catch {
      return null;
    }
  }, []);
  const [loginForm] = Form.useForm(),
    [editForm] = Form.useForm(),
    [settingsForm] = Form.useForm(),
    [passwordForm] = Form.useForm();
  const load = async () => {
    try {
      const d = await api("/admin");
      setData(d);
      setUnauthorized(false);
      setError("");
    } catch (e) {
      if (e.status === 401) {
        setUnauthorized(true);
        setData(null);
      } else setError(e.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    if (!data) return;
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [!!data]);
  const act = async (fn, success) => {
    setBusy(true);
    try {
      await fn();
      if (success) message.success(success);
      await load();
    } catch (e) {
      message.error(e.message);
      if (e.status === 401) setUnauthorized(true);
    } finally {
      setBusy(false);
    }
  };
  const openEdit = (r) => {
    setEdit(r);
    editForm.resetFields();
    const fields =
      r.kind === "job"
        ? [
            "company",
            "category",
            "positions",
            "location",
            "education",
            "salary",
            "method",
            "application",
            "apply_url",
            "announcement_url",
            "notes",
            "updated",
            "deadline",
            "start",
          ]
        : ["company", "location", "time_text", "notes", "apply_url"];
    const values = {};
    for (const f of fields) values[f] = r[f] || "";
    if (r.kind === "job") {
      values.deadline = r.deadline_date || r.deadline || "";
      values.start = r.start_date || r.start || "";
    }
    setEditInitial(values);
    editForm.setFieldsValue(values);
  };
  if (loading)
    return (
      <>
        <Header admin />
        <main className="page">
          <Skeleton active />
        </main>
      </>
    );
  if (unauthorized)
    return (
      <>
        <Header admin />
        <div className="login-shell">
          <Card className="login-card">
            <Avatar
              size={52}
              className="login-icon"
              icon={<SafetyCertificateOutlined />}
            />
            <Title level={2}>管理控制台</Title>
            <Paragraph type="secondary">
              维护信息，查看同步状态，让机会持续更新。
            </Paragraph>
            <Form
              form={loginForm}
              layout="vertical"
              initialValues={{
                username: savedAdminLogin?.username || "admin",
                password: savedAdminLogin?.password || "",
                remember: !!savedAdminLogin,
              }}
              onFinish={async (values) => {
                setBusy(true);
                try {
                  const { remember, ...credentials } = values;
                  await api("/login", "POST", credentials);
                  if (remember) {
                    localStorage.setItem("job-admin-login", JSON.stringify(credentials));
                  } else {
                    localStorage.removeItem("job-admin-login");
                  }
                  await load();
                } catch (e) {
                  message.error(e.message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              <Form.Item
                label="管理员账号"
                name="username"
                rules={[{ required: true, message: "请输入账号" }]}
              >
                <Input autoComplete="username" placeholder="管理员账号" />
              </Form.Item>
              <Form.Item
                label="密码"
                name="password"
                rules={[{ required: true, message: "请输入密码" }]}
              >
                <Input.Password
                  autoComplete="current-password"
                  placeholder="输入管理密码"
                />
              </Form.Item>
              <Form.Item name="remember" valuePropName="checked">
                <Checkbox>在此设备保存管理员密码</Checkbox>
              </Form.Item>
              <Paragraph type="secondary">
                密码保存在当前浏览器中，仅建议在自己的固定设备上启用。
              </Paragraph>
              <Button
                type="primary"
                htmlType="submit"
                block
                loading={busy}
                size="large"
              >
                登录管理后台 <ArrowRightOutlined />
              </Button>
            </Form>
            <div className="login-foot">
              <Link href="/">返回招聘信息</Link>
              <Text type="secondary">
                <SafetyCertificateOutlined /> 安全会话
              </Text>
            </div>
          </Card>
        </div>
      </>
    );
  if (!data)
    return (
      <Result
        status="error"
        title="后台加载失败"
        subTitle={error}
        extra={<Button onClick={load}>重试</Button>}
      />
    );
  const rows = data.records.filter(
    (r) =>
      r.kind === kind &&
      (!query ||
        [r.company, r.positions, r.location].some((x) => x?.includes(query))),
  );
  const columns = [
    {
      title: "单位 / 活动",
      dataIndex: "company",
      width: 300,
      render: (v, r) => (
        <>
          <Text strong>{v}</Text>
          <div>
            <Text type="secondary">
              {r.source === "manual" ? "人工补充" : `原表第 ${r.source_row} 行`}
            </Text>{" "}
            {r.modified && <Tag>已调整</Tag>}
          </div>
        </>
      ),
    },
    { title: "状态", width: 130, render: (_, r) => statusTag(r) },
    {
      title: "展示",
      width: 90,
      render: (_, r) => (
        <Switch
          checked={!r.hidden}
          checkedChildren="显示"
          unCheckedChildren="隐藏"
          onChange={(v) =>
            act(
              () => api("/admin/records/" + r.id, "PATCH", { hidden: !v }),
              "展示状态已更新",
            )
          }
        />
      ),
    },
    {
      title: "置顶",
      width: 75,
      render: (_, r) => (
        <Switch
          checked={!!r.pinned}
          size="small"
          aria-label={"置顶 " + r.company}
          onChange={(v) =>
            act(
              () => api("/admin/records/" + r.id, "PATCH", { pinned: v }),
              "置顶状态已更新",
            )
          }
        />
      ),
    },
    {
      title: "操作",
      width: 175,
      render: (_, r) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(r)}
          >
            编辑
          </Button>
          {r.modified && (
            <Popconfirm
              title="恢复到原表 / 初始内容？"
              description="这会撤销此记录的编辑、隐藏和置顶设置。"
              onConfirm={() =>
                act(
                  () => api("/admin/records/" + r.id + "/override", "DELETE"),
                  "已恢复",
                )
              }
            >
              <Button size="small" type="text">
                恢复
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];
  const fields =
    edit?.kind === "job"
      ? [
          ["company", "单位名称"],
          ["positions", "招聘岗位"],
          ["location", "工作地点"],
          ["education", "学历要求"],
          ["category", "单位类型"],
          ["salary", "薪资情况"],
          ["start", "开始日期"],
          ["deadline", "截止日期"],
          ["method", "投递方式"],
          ["application", "投递说明"],
          ["apply_url", "投递链接（http / https / mailto）"],
          ["announcement_url", "公告链接"],
          ["notes", "补充信息 / 内推码"],
          ["updated", "信息更新时间"],
        ]
      : [
          ["company", "宣讲会 / 企业名称"],
          ["time_text", "宣讲时间（例如 2026-09-21 14:00-16:00）"],
          ["location", "地点"],
          ["notes", "补充说明"],
          ["apply_url", "报名链接"],
        ];
  const reviewRows = data.reviews.filter(
    (item) =>
      (!reviewQuery || item.company.includes(reviewQuery)) &&
      (reviewStatus === "all" || item.status === reviewStatus),
  );
  const reviewPendingCount = data.reviews.filter((item) =>
    ["queued", "running"].includes(item.status),
  ).length;
  const reviewStatusTag = (status) => {
    const values = {
      unreviewed: ["未分析", "default"],
      queued: ["排队中", "blue"],
      running: ["分析中", "processing"],
      success: ["已完成", "green"],
      error: ["失败", "red"],
    };
    const [label, color] = values[status] || [status, "default"];
    return <Tag color={color}>{label}</Tag>;
  };
  const reviewColumns = [
    {
      title: "公司",
      dataIndex: "company",
      width: screens.md ? 220 : 150,
      render: (value, item) => (
        <>
          <Text strong>{value}</Text>
          {!screens.md && (
            <div className="mobile-review-state">
              {reviewStatusTag(item.status)}
              {item.score !== null && <ReviewTag review={item} compact />}
            </div>
          )}
          {item.error && (
            <Paragraph className="review-error" ellipsis={{ rows: 2, tooltip: item.error }}>
              {item.error}
            </Paragraph>
          )}
        </>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      responsive: ["md"],
      render: reviewStatusTag,
    },
    {
      title: "推荐参考",
      width: 160,
      responsive: ["md"],
      render: (_, item) =>
        item.score === null ? <Text type="secondary">—</Text> : <ReviewTag review={item} compact />,
    },
    {
      title: "来源",
      width: 80,
      responsive: ["md"],
      render: (_, item) => (item.sources?.length ? `${item.sources.length} 个` : "—"),
    },
    {
      title: "用量",
      width: 120,
      responsive: ["lg"],
      render: (_, item) =>
        item.usage?.total_tokens
          ? `${item.usage.total_tokens.toLocaleString()} Token`
          : "—",
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 140,
      responsive: ["lg"],
      render: (value) => (value ? fmt(value, "MM-DD HH:mm") : "—"),
    },
    {
      title: "操作",
      width: screens.md ? 110 : 74,
      fixed: screens.md ? "right" : undefined,
      render: (_, item) => {
        const pending = ["queued", "running"].includes(item.status);
        return (
          <Button
            size="small"
            loading={item.status === "running"}
            disabled={
              !data.review_config.configured ||
              pending ||
              busy ||
              reviewPendingCount >= 5
            }
            onClick={() =>
              act(
                () => api("/admin/reviews/analyze", "POST", { company: item.company }),
                item.score === null ? "已加入分析队列" : "已加入重新分析队列",
              )
            }
          >
            {pending
              ? item.status === "running"
                ? "分析中"
                : "已排队"
              : item.score === null
                ? "分析"
                : screens.md
                  ? "重新分析"
                  : "重新"}
          </Button>
        );
      },
    },
  ];
  return (
    <>
      <Header admin config={data.config} />
      <main className="page admin-page">
        <div className="admin-heading">
          <div>
            <div className="eyebrow">MANAGEMENT</div>
            <Title level={2}>管理控制台</Title>
            <Text type="secondary">
              管理展示内容与同步。人工调整独立保存，不回写原表。
            </Text>
          </div>
          <Space>
            <Tag icon={<SafetyCertificateOutlined />}>{data.username}</Tag>
            <Button
              icon={<LogoutOutlined />}
              onClick={() => act(() => api("/logout", "POST"), "已退出")}
            >
              退出
            </Button>
          </Space>
        </div>
        {error && <Alert type="error" message={error} showIcon />}
        {data.last_error && (
          <Alert
            type="warning"
            message="最近一次同步失败，线上保留之前的数据"
            description={data.last_error}
            showIcon
          />
        )}
        <Row gutter={[16, 16]} className="stats">
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="招聘信息"
                value={data.records.filter((r) => r.kind === "job").length}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="宣讲会信息"
                value={data.records.filter((r) => r.kind === "event").length}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="已隐藏"
                value={data.records.filter((r) => r.hidden).length}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="自动同步周期" value="15" suffix="分钟" />
            </Card>
          </Col>
        </Row>
        <Card className="admin-main">
          <Tabs
            defaultActiveKey="records"
            onChange={(key) => {
              if (key === "settings") settingsForm.setFieldsValue(data.config);
            }}
            items={[
              {
                key: "records",
                label: (
                  <Space>
                    <BankOutlined />
                    信息管理
                  </Space>
                ),
                children: (
                  <>
                    <div className="admin-toolbar">
                      <Segmented
                        value={kind}
                        onChange={setKind}
                        options={[
                          { value: "job", label: "招聘信息" },
                          { value: "event", label: "宣讲会信息" },
                        ]}
                      />
                      <Input
                        prefix={<SearchOutlined />}
                        placeholder="搜索单位、岗位、地点"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        allowClear
                      />
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => openEdit({ kind })}
                      >
                        新增信息
                      </Button>
                    </div>
                    <Table
                      rowKey="id"
                      columns={columns}
                      dataSource={rows}
                      scroll={{ x: 780 }}
                      pagination={{
                        pageSize: 10,
                        showSizeChanger: false,
                        showTotal: (t) => `共 ${t} 条`,
                      }}
                    />
                    <Alert
                      type="info"
                      showIcon
                      message="隐藏的内容仍保留在后台，可随时重新展示。原表记录移除后仍作为历史信息保留；人工新增内容不受同步影响。"
                    />
                  </>
                ),
              },
              {
                key: "reviews",
                label: (
                  <Space>
                    <RadarChartOutlined />
                    网评分析
                  </Space>
                ),
                children: (
                  <>
                    <Alert
                      type={data.review_config.configured ? "success" : "warning"}
                      showIcon
                      message={
                        data.review_config.configured
                          ? `分析服务已就绪：${data.review_config.name}`
                          : "网评分析服务尚未配置"
                      }
                      description={
                        data.review_config.configured
                          ? `当前模型：${data.review_config.model}。搜索结果和模型总结会在后台队列中逐家公司处理。`
                          : `当前模式：${data.review_config.name}。待配置：${data.review_config.missing.join("、")}。百炼 Responses API 会通过内置 web_search 搜索并返回真实来源链接；密钥只保存在服务器。`
                      }
                    />
                    <div className="admin-toolbar review-toolbar">
                      <Space wrap>
                        <Input
                          prefix={<SearchOutlined />}
                          placeholder="搜索公司"
                          value={reviewQuery}
                          onChange={(event) => setReviewQuery(event.target.value)}
                          allowClear
                        />
                        <Select
                          value={reviewStatus}
                          onChange={setReviewStatus}
                          options={[
                            { value: "all", label: "全部状态" },
                            { value: "unreviewed", label: "未分析" },
                            { value: "queued", label: "排队中" },
                            { value: "running", label: "分析中" },
                            { value: "success", label: "已完成" },
                            { value: "error", label: "失败" },
                          ]}
                        />
                      </Space>
                      <Space wrap>
                        <Popconfirm
                          title="分析 5 家未分析公司？"
                          description="每次最多加入 5 家，完成并检查用量后再继续。"
                          onConfirm={() =>
                            act(
                              () => api("/admin/reviews/batch", "POST", { mode: "missing", max_items: 5 }),
                              "未分析公司已加入队列",
                            )
                          }
                        >
                          <Button
                            loading={busy}
                            disabled={
                              !data.review_config.configured || busy || reviewPendingCount >= 5
                            }
                          >
                            补齐未分析
                          </Button>
                        </Popconfirm>
                        <Popconfirm
                          title="更新 5 家过期分析？"
                          description="每次最多加入 5 家，旧结果在更新成功前仍可展示。"
                          onConfirm={() =>
                            act(
                              () => api("/admin/reviews/batch", "POST", { mode: "stale", max_items: 5 }),
                              "过期分析已加入队列",
                            )
                          }
                        >
                          <Button
                            loading={busy}
                            disabled={
                              !data.review_config.configured || busy || reviewPendingCount >= 5
                            }
                          >
                            更新过期内容
                          </Button>
                        </Popconfirm>
                      </Space>
                    </div>
                    <Table
                      rowKey="company"
                      size="small"
                      columns={reviewColumns}
                      dataSource={reviewRows}
                      scroll={{ x: screens.md ? 820 : 224 }}
                      pagination={{
                        pageSize: 10,
                        showSizeChanger: false,
                        showTotal: (total) => `共 ${total} 家公司`,
                      }}
                    />
                    <Alert
                      type="info"
                      showIcon
                      message="推荐程度仅是公开网评的量化参考。详情页会同时展示总结、常见正面反馈、常见顾虑、分析时间和可点击来源。"
                    />
                  </>
                ),
              },
              {
                key: "sync",
                label: (
                  <Space>
                    <SyncOutlined />
                    数据同步
                  </Space>
                ),
                children: (
                  <>
                    <div className="admin-toolbar">
                      <div>
                        <Title level={4}>金山文档同步</Title>
                        <Text type="secondary">
                          每 15 分钟检查两个工作表，成功后一次性更新数据。
                        </Text>
                      </div>
                      <Button
                        type="primary"
                        loading={data.sync_running || busy}
                        icon={<SyncOutlined />}
                        onClick={() =>
                          act(
                            () => api("/admin/sync", "POST"),
                            "同步任务已启动",
                          )
                        }
                      >
                        立即同步
                      </Button>
                    </div>
                    <Descriptions
                      column={1}
                      items={[
                        {
                          key: "url",
                          label: "数据源",
                          children: (
                            <Link href={data.config.source_url} target="_blank">
                              {data.config.source_url}
                            </Link>
                          ),
                        },
                        {
                          key: "state",
                          label: "自动同步",
                          children: (
                            <Tag
                              color={data.config.auto_sync ? "green" : "orange"}
                            >
                              {data.config.auto_sync
                                ? "已启用 · 15 分钟"
                                : "已暂停"}
                            </Tag>
                          ),
                        },
                      ]}
                    />
                    <Table
                      rowKey="id"
                      size="small"
                      dataSource={data.logs}
                      scroll={{ x: 680 }}
                      pagination={{ pageSize: 10, showSizeChanger: false }}
                      columns={[
                        {
                          title: "时间",
                          dataIndex: "started",
                          render: (v) => fmt(v, "MM-DD HH:mm:ss"),
                        },
                        {
                          title: "结果",
                          dataIndex: "status",
                          render: (v) => (
                            <Tag
                              color={
                                {
                                  success: "green",
                                  running: "blue",
                                  error: "red",
                                }[v]
                              }
                            >
                              {
                                {
                                  success: "成功",
                                  running: "同步中",
                                  error: "失败",
                                }[v]
                              }
                            </Tag>
                          ),
                        },
                        { title: "招聘", dataIndex: "jobs" },
                        { title: "宣讲会", dataIndex: "events" },
                        { title: "变化", dataIndex: "changed" },
                        { title: "说明", dataIndex: "message", width: 270 },
                      ]}
                    />
                  </>
                ),
              },
              {
                key: "notifications",
                label: (
                  <Space>
                    <BellOutlined />
                    消息推送
                  </Space>
                ),
                children: (
                  <>
                    <Alert
                      type={data.pushplus.configured ? "success" : "warning"}
                      showIcon
                      message={data.pushplus.configured ? "PushPlus 已配置" : "PushPlus 尚未配置"}
                      description={
                        data.pushplus.configured
                          ? "同步发现新增宣讲会时，只推送企业名称和宣讲时间。Token 仅保存在服务器环境变量中。"
                          : "请在服务器 .env 中配置 PUSHPLUS_TOKEN 后重启服务。"
                      }
                    />
                    <Card size="small" style={{ marginTop: 16 }}>
                      <Descriptions
                        column={1}
                        items={[
                          {
                            key: "state",
                            label: "自动推送",
                            children: (
                              <Switch
                                checked={data.pushplus.enabled}
                                disabled={!data.pushplus.configured || busy}
                                checkedChildren="启用"
                                unCheckedChildren="暂停"
                                onChange={(enabled) =>
                                  act(
                                    () => api("/admin/pushplus", "PUT", { enabled }),
                                    enabled ? "PushPlus 推送已启用" : "PushPlus 推送已暂停",
                                  )
                                }
                              />
                            ),
                          },
                          {
                            key: "success",
                            label: "最近成功",
                            children: data.pushplus.last_success
                              ? fmt(data.pushplus.last_success, "YYYY-MM-DD HH:mm:ss")
                              : "暂无",
                          },
                          {
                            key: "error",
                            label: "最近错误",
                            children: data.pushplus.last_error || "无",
                          },
                        ]}
                      />
                      <Button
                        icon={<BellOutlined />}
                        disabled={!data.pushplus.configured || busy}
                        loading={busy}
                        onClick={() =>
                          act(
                            () => api("/admin/pushplus/test", "POST"),
                            "PushPlus 测试消息已发送",
                          )
                        }
                      >
                        发送测试消息
                      </Button>
                    </Card>
                  </>
                ),
              },
              {
                key: "settings",
                label: (
                  <Space>
                    <SettingOutlined />
                    站点设置
                  </Space>
                ),
                children: (
                  <Form
                    form={settingsForm}
                    initialValues={data.config}
                    layout="vertical"
                    className="settings-form"
                    onFinish={(values) =>
                      act(
                        () =>
                          api("/admin/settings", "PUT", {
                            ...data.config,
                            ...values,
                          }),
                        "设置已保存",
                      )
                    }
                  >
                    <Form.Item
                      name="title"
                      label="站点名称"
                      rules={[{ required: true }]}
                    >
                      <Input maxLength={60} />
                    </Form.Item>
                    <Form.Item
                      name="source_url"
                      label="金山文档分享链接"
                      extra="原表需要允许任何人查看，包含“招聘信息”和“宣讲会信息”工作表及兼容表头。"
                      rules={[
                        { required: true },
                        {
                          pattern:
                            /^https:\/\/www\.kdocs\.cn\/l\/[A-Za-z0-9]+$/,
                          message: "请输入标准金山文档分享链接",
                        },
                      ]}
                    >
                      <Input />
                    </Form.Item>
                    <Form.Item
                      name="auto_sync"
                      label="每 15 分钟自动同步"
                      valuePropName="checked"
                    >
                      <Switch />
                    </Form.Item>
                    <Button htmlType="submit" type="primary" loading={busy}>
                      保存设置
                    </Button>
                  </Form>
                ),
              },
              {
                key: "security",
                label: (
                  <Space>
                    <SafetyCertificateOutlined />
                    安全设置
                  </Space>
                ),
                children: (
                  <>
                    <Form
                      form={passwordForm}
                      layout="vertical"
                      className="settings-form"
                      onFinish={(values) =>
                        act(
                          async () => {
                            await api("/admin/password", "PUT", {
                              current: values.current,
                              password: values.password,
                            });
                            localStorage.removeItem("job-admin-login");
                          },
                          "密码已修改，请重新登录",
                        )
                      }
                    >
                      <Title level={4}>修改管理员密码</Title>
                      <Paragraph type="secondary">
                        修改后，所有已登录的管理会话都会失效。
                      </Paragraph>
                      <Form.Item
                        name="current"
                        label="当前密码"
                        rules={[{ required: true }]}
                      >
                        <Input.Password autoComplete="current-password" />
                      </Form.Item>
                      <Form.Item
                        name="password"
                        label="新密码"
                        rules={[
                          {
                            required: true,
                            min: 12,
                            message: "请使用至少 12 位密码",
                          },
                        ]}
                      >
                        <Input.Password autoComplete="new-password" />
                      </Form.Item>
                      <Form.Item
                        name="confirm"
                        label="再次输入新密码"
                        dependencies={["password"]}
                        rules={[
                          { required: true },
                          ({ getFieldValue }) => ({
                            validator: (_, v) =>
                              !v || getFieldValue("password") === v
                                ? Promise.resolve()
                                : Promise.reject(new Error("两次密码不一致")),
                          }),
                        ]}
                      >
                        <Input.Password autoComplete="new-password" />
                      </Form.Item>
                      <Button type="primary" htmlType="submit" loading={busy}>
                        修改密码并退出登录
                      </Button>
                    </Form>
                    <Divider />
                    <Title level={5}>最近管理操作</Title>
                    <Table
                      rowKey="id"
                      size="small"
                      pagination={{ pageSize: 5, showSizeChanger: false }}
                      dataSource={data.audit}
                      columns={[
                        {
                          title: "时间",
                          dataIndex: "at",
                          render: (v) => fmt(v, "MM-DD HH:mm:ss"),
                        },
                        { title: "操作", dataIndex: "action" },
                      ]}
                    />
                  </>
                ),
              },
            ]}
          />
        </Card>
      </main>
      <Modal
        title={edit?.id ? "编辑信息" : "新增信息"}
        open={!!edit}
        width={680}
        onCancel={() => setEdit(null)}
        onOk={() => editForm.submit()}
        confirmLoading={busy}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={async (values) => {
            setBusy(true);
            try {
              const body = Object.fromEntries(
                Object.entries(values)
                  .map(([k, v]) => [k, v || ""])
                  .filter(([k, v]) => !edit.id || v !== editInitial[k]),
              );
              if (edit.id && !Object.keys(body).length) {
                setEdit(null);
                return;
              }
              await api(
                edit.id ? "/admin/records/" + edit.id : "/admin/records",
                edit.id ? "PATCH" : "POST",
                edit.id ? body : { ...body, kind: edit.kind },
              );
              setEdit(null);
              message.success("已保存");
              await load();
            } catch (e) {
              message.error(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {fields.map(([name, label]) => (
            <Form.Item
              key={name}
              name={name}
              label={label}
              rules={
                ["company", "time_text"].includes(name)
                  ? [{ required: true, message: "请填写" + label }]
                  : []
              }
            >
              <Input.TextArea
                autoSize={{
                  minRows: ["positions", "notes", "application"].includes(name)
                    ? 3
                    : 1,
                  maxRows: 8,
                }}
              />
            </Form.Item>
          ))}
        </Form>
      </Modal>
    </>
  );
}

class ErrorBoundary extends React.Component {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <Result
        status="error"
        title="页面出现问题"
        subTitle="请刷新页面后重试。"
        extra={<Button onClick={() => location.reload()}>刷新</Button>}
      />
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")).render(
  <ConfigProvider locale={zhCN} theme={theme}>
    <AntApp>
      <ErrorBoundary>
        {location.pathname.startsWith("/admin") ? (
          <AdminPage />
        ) : (
          <PublicPage />
        )}
      </ErrorBoundary>
    </AntApp>
  </ConfigProvider>,
);
