# ModelScope 创空间（Studios）全量 UI 视觉与交互动画设计规范

> **基准采样源**：`https://modelscope.cn/studios`（1440×900 桌面视口实机样式与交互采样）  
> **适用范围**：**浣溪AI Lab** 全平台四端原型与后续前端工程实现（① 学生端 · ② 学院端 · ③ 运营管理端·学校用 · ④ 平台管理端·浣溪内部 · 原型总览台）  
> **关联架构决策**：[ADR 0003 · UI 与交互设计规范](./adr/0003-ui-conventions.md)  
> **版本状态**：V2.0 · ModelScope Studios 全量复刻规范（2026-11-26）

---

## 1. 设计哲学与核心视觉气质

ModelScope 创空间（魔搭社区 Studios）建立了一套极具辨识度的 **「极光漫射画布（Pastel Aurora Mesh）+ 冰云微物软壳（Cloud Soft-Shell）+ 破界微倾交互（Overhanging Tilt Micro-Interaction）」** 设计语言：

1. **轻盈通透的纯白与冰云分层**：页面主背景坚持使用纯净 `#FFFFFF`，卡片与筛选胶囊不依赖深色描边或厚重阴影，而是采用 `#F7F9FD`（冰云浅蓝灰）无感软壳承载，悬停时微沉至 `#F3F5FA`（外轮廓 `#EFF2F9`），形成克制而细腻的触觉层次。
2. **宇宙靛蓝（`#27254C`）替代纯黑，电光魔搭紫（`#624AFF`）点睛**：所有主标题、激活态文字与深色胶囊按钮统一采用带有微紫调的深邃靛蓝 `#27254C`，与品牌主色 `#624AFF` 及青紫渐变（`#624AFF → #36CFD1`）天然同源，既具科技先锋感又不刺眼。
3. **破界几何结构打破呆板栅格**：
   - **首屏反向内凹切角台面（Notched Concave Shelf）**：白色内容区左上方凸起为 `24px` 圆角标签台（承载「精选内容」），右侧通过 `24px` 反向内凹圆弧平滑过渡到浅薰衣草极光底，紧邻深紫→电光紫渐变胶囊主按钮。
   - **外悬缩略图网格卡（Overhanging Thumbnail Card）**：16:9 圆角封面向上突破卡片顶边 `-21px` 悬浮，携带浅紫蓝弥散投影；鼠标悬停时缩略图逆时针微倾 **`rotate(-6deg)`** 并隐去白边，赋予 AI 应用卡片鲜活的陈列把玩感。
   - **环形星轨热榜轮播（Orbital Arc Carousel）**：首屏右侧以椭圆形弧线排布 Top 热门应用缩略图，主卡居中放大，两侧卡片按弧度依次旋转递减，底部配紫金麦穗勋章与悬停显影「去体验」按钮。

---

## 2. 全量色彩系统（Color System & CSS Tokens）

### 2.1 核心品牌色与语义色

| Token 变量名 | 色值 | 原始对照 | 语义与使用场景 |
| :--- | :--- | :--- | :--- |
| `--ms-primary` | `#624AFF` | `--ant-color-primary` | **品牌电光紫**：Hero「创空间」字标、激活筛选文字、输入框聚焦描边、计数徽章、`xGPU` 算力标识 |
| `--ms-primary-hover` | `#8A73FF` | `--ant-color-primary-hover` | 主色悬停高亮态 |
| `--ms-primary-active` | `#4434D9` | `--ant-color-primary-active` | 主色按压激活态 |
| `--ms-primary-bg` | `#F4F0FF` | `--ant-color-primary-bg` | 浅紫品牌弱底、高亮提示块背景 |
| `--ms-primary-selected` | `#F1F1FD` | `.is-selected` pill bg | **筛选胶囊与侧边菜单选中态底色**（配 `#624AFF` 文字） |
| `--ms-primary-border` | `#D2C4FF` | `--ant-color-primary-border` | 品牌浅紫描边（悬停 `#AF9CFF`） |
| `--ms-focus-ring` | `#B2A6FF` | `0 0 0 3px #B2A6FF` | 顶部工具胶囊与关键控件焦点光环 |
| `--ms-cyan` | `#36CFD1` | `.acss-r8dsx6` stop | **极光青辅色**：与 `#624AFF` 组成 `91deg` 文字渐变及图表双序列对比色 |
| `--ms-deep-violet` | `#36298C` | `.acss-1ttgtew` | 热榜勋章深紫锚点色（`180deg` 渐变至 `#624AFF`） |
| `--ms-status-blue` | `#3D75B2` | `.acss-1ndbh92` | 卡片内联状态文字色（如「仅公开体验」「休眠中」「在线体验」） |
| `--ms-status-blue-bg` | `#EFF2F9` | `.acss-1ndbh92` bg | 卡片内联状态胶囊浅冰蓝底（休眠态可用 `rgba(61, 117, 178, 0.12)`） |
| `--ms-success` | `#52C41A` / `#13C2C2` | `--ant-color-success` / `cyan` | 运行正常、已上架、调用成功指标 |
| `--ms-warning` | `#FAAD14` | `--ant-color-warning` | 审核中、待审批、预算水位预警（浅底 `#FFFBE6` / 文字 `#AD6800`） |
| `--ms-error` | `#FF4D4F` | `--ant-color-error` | 高危风控告警、一键冻结、驳回需修改（浅底 `#FFF2F0` / 文字 `#CF1322`） |

### 2.2 中性色、表面层级与分割线

| Token 变量名 | 色值 | 语义与使用规范 |
| :--- | :--- | :--- |
| `--ms-bg-base` | `#FFFFFF` | 页面主画布、顶栏、吸顶筛选栏背景、精选卡左内嵌白卡、网格卡内部 Tag 底色 |
| `--ms-surface-card` | `#F7F9FD` | **核心软壳色**：创空间网格卡默认底色、吸顶内联搜索框底色、筛选胶囊默认底色 |
| `--ms-surface-hover` | `#F3F5FA` | **核心悬停软壳色**：创空间网格卡 `:hover` 底色、次级按钮悬停底色、骨架屏基色 |
| `--ms-surface-subtle` | `#EFF2F9` | 顶栏图标按钮悬停底色、网格卡悬停外轮廓色（`outline-color`）、次级按压态 |
| `--ms-ink` | `#27254C` | **主文字宇宙靛蓝**：H1–H3 标题、卡片标题、激活导航项、搜索提交深色按钮 |
| `--ms-ink-body` | `#464D5B` | **正文与元数据铅灰蓝**：默认导航文字、筛选胶囊默认文字、卡片日期/浏览/点赞/@作者 |
| `--ms-ink-muted` | `#8284A4` | **次要说明雾紫灰**：Hero 宽字距副标题、搜索框 Placeholder、辅助图标、空状态提示 |
| `--ms-border` | `#ECEDF1` | **全局 `1px` 极细分割线**：Hero 搜索框默认边框、卡片元数据竖向短分割线（`1px × 11px`）、精选卡外框、页脚顶部分割线 |
| `--ms-tag-overflow-bg` | `rgba(175, 156, 255, 0.10)` | 卡片标签溢出 `...` 触发块浅紫半透明底 |

### 2.3 招牌渐变与光影配方（可直接复用 CSS）

```css
:root {
  /* 1. 首屏六层极光漫射网格背景 (Hero Pastel Aurora Mesh) */
  --ms-hero-mesh:
    linear-gradient(180deg, #FFFFFF 0%, rgba(255, 255, 255, 0) 24%),
    linear-gradient(180deg, rgba(242, 240, 255, 0) 32%, #F2F0FF 88%),
    radial-gradient(ellipse 55% 55% at 0% -5%, rgba(203, 228, 255, 0.85), transparent 98%),
    radial-gradient(ellipse 70% 60% at 100% 12%, rgba(211, 245, 254, 0.72), transparent 76%),
    radial-gradient(ellipse 68% 48% at 0% 100%, rgba(242, 240, 255, 0.82), transparent 72%),
    radial-gradient(ellipse 42% 36% at 6% 0%, rgba(251, 229, 255, 0.72), transparent 65%),
    linear-gradient(129deg, transparent -33%, rgba(211, 245, 254, 0.58) 63%, #F7FDFF 143%),
    #FFFFFF;

  /* 2. 主行动按钮渐变（「创建创空间 / 发布新作品」） */
  --ms-cta-gradient: linear-gradient(90deg, #27254C 0%, #624AFF 100%);

  /* 3. 导航先锋功能渐变字 */
  --ms-text-gradient: linear-gradient(91deg, #624AFF 0%, #36CFD1 100%);

  /* 4. 热榜排名数字渐变 */
  --ms-rank-gradient: linear-gradient(180deg, #36298C 51%, #624AFF 100%);

  /* 5. 外悬缩略图与高级浮层弥散紫蓝阴影 */
  --ms-thumb-shadow: 0 12px 21.6px rgba(144, 156, 234, 0.25);

  /* 6. 卡片内白底 Tag 悬停微浮阴影 */
  --ms-tag-hover-shadow: 0 2px 12px 0 rgba(150, 150, 176, 0.30);

  /* 7. 星轨热榜浮层阴影 */
  --ms-popover-shadow: 0 2px 32px rgba(39, 37, 76, 0.08);
}
```

### 2.4 精选内容外框环境色池 & 无图预设渐变封面池

- **精选卡片（`data-variant="featured"`）环境主色池**（外框 `background` 提取自封面主色调）：
  - 鼠尾草绿 `rgb(148, 163, 130)` · 深皇家蓝 `rgb(32, 62, 147)` · 银雾紫灰 `rgb(219, 220, 220)`
  - 幻彩丁香 `rgb(214, 199, 240)` · 冰川岩蓝 `rgb(211, 219, 228)` · 晴空碧青 `rgb(199, 232, 239)`
- **无自定义截图时的预设极光封面（Preset Cover Gradients）**：
  - 预设 A（星云紫蓝）：`linear-gradient(135deg, #4D6BFF 0%, #837DFF 48%, #6CE5E8 100%)`
  - 预设 B（极光粉紫）：`linear-gradient(135deg, #B892FF 0%, #E0B5FF 50%, #9BD9FF 100%)`
  - 预设 C（深海流光）：`linear-gradient(135deg, #1E3A5F 0%, #3D75B2 52%, #8BE0E8 100%)`
  - 中央统一叠加纯白几何花章 SVG 图标。

---

## 3. 字体排印体系（Typography Scale · 无外链系统级兼容栈）

为保证国内网络环境零外部字体阻塞（禁止引入 `googleapis` / `gstatic` 外链），采用本机等宽与中文无衬线高保真混排栈：

- **全局基础字体栈（`body`）**：  
  `'PingFang SC', 'Microsoft Yahei', 'Helvetica Neue', Helvetica, Arial, -apple-system, BlinkMacSystemFont, sans-serif`  
  全局默认开启等宽表格数字：`font-variant-numeric: tabular-nums; font-feature-settings: "tnum";`
- **创空间标题混排字体栈（`.ms-title-font`）**：  
  `'SFMono-Regular', ui-monospace, Menlo, Monaco, Consolas, 'PingFang SC', 'Microsoft Yahei', sans-serif`
- **代码/密钥/指标数字栈（`.mono`）**：  
  `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`

| 层级 | 元素定位 | 字号 | 字重 | 行高 | 字距 (`letter-spacing`) | 颜色 |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **Display H1** | Hero 主标题（如 `浣溪AI` + `创空间广场`） | `32px–33px` | `800` | `36px` | EN `0.05em` / CN `0.08em` | `#27254C` + `#624AFF` |
| **Rank XL** | 星轨热榜名次数字（`1`） | `28px–31.8px` | `800` | `1.0` | `0` | `--ms-rank-gradient` |
| **KPI Mono** | 数据门户/大盘核心指标大字 | `26px–32px` | `700` | `34px` | `-0.02em` | `#27254C` |
| **Section H2-A** | 台面一级栏目标题（`精选内容`） | `20px` | `600` | `28px` | `0` | `#27254C` |
| **Orbit Title** | 星轨热榜当前聚焦作品名 | `19px–20px` | `700` | `22px` | `0` | `#27254C` |
| **Section H2-B** | 吸顶栏目标题（`更多精彩创空间`） | `18px` | `600` | `30px` | `0` | `#27254C` |
| **Card Title** | **精选卡 / 网格卡标题（`.ms-title-font`）** | `16px–17px` | `600` | `22px` | `-0.01em` | `#27254C`（单行省略） |
| **Nav Item** | 顶部全局导航项 | `14px–15px` | `400` / 激活 `600` | `60px` | `0` | `#464D5B` → `#27254C` |
| **Hero Subtitle** | Hero 呼吸感宽距副标题 | `13px–14px` | `400` | `20px` | **`6.5px–9px`（中文）** | `#8284A4` |
| **Input / Body** | 搜索框输入、表单正文、评论正文 | `13px` | `400` | `20px` | `0` | `#27254C` / `#464D5B` |
| **Pill & Meta** | 筛选维度标签、筛选胶囊、底部日期/浏览/点赞/@作者 | `12px` | 标签 `500` / 胶囊 `400` | `16px–20px` | `0` | `#464D5B`（选中 `#624AFF`） |
| **Micro Chip** | 卡片内部白底标签 / 内联状态徽章 | `11px` / `10px` | `400` / `500` | `18px` / `12px` | `0` | `#464D5B` / `#3D75B2` |

---

## 4. 栅格布局、容器尺度与间距纪律

1. **视口与内容容器**：标准桌面视口 `1440px` 对应有效内容宽度 `1280px`（`max-w-7xl` 居中响应）。
2. **顶部导航栏（Global Header）**：高度固定 **`60px`**，`background: rgba(255, 255, 255, 0.90)`，`backdrop-filter: blur(20px)`。
3. **Hero 与反向内凹切角台面（Hero + Notched Shelf）**：
   - 左下角白色凸起台面高度 `54px`，左上/右上圆角 `24px 24px 0 0`，右侧衔接 `24px × 24px` 反向内凹伪元素：`radial-gradient(circle at top right, transparent 24px, #FFFFFF 24.5px)`。
4. **双网格卡片间距差异**：
   - **精选内容网格（Featured Grid）**：桌面 **3 列**，间距 `20px`，卡片高度 **`124px`**，圆角 `16px`。
   - **创空间主网格（Overhanging Studio Grid）**：`<768px` 1 列 · `768px–1023px` 2 列 · `≥1024px` **3 列**；水平间距 **`20px`**，**垂直行距必须设为 `41px`（首行 `padding-top: 22px`）**，容纳向上破界溢出 `-21px` 的 16:9 封面缩略图。

---

## 5. 标志性组件解剖与交互动画规范

| 交互对象 | 触发条件 | 变换属性与目标值 | 时长与缓动曲线（Easing） |
| :--- | :--- | :--- | :--- |
| **网格卡外悬封面微倾** | `.ms-grid-card:hover .ms-card-thumb` | `transform: rotate(-6deg)`；内部图片 `border-color: transparent` | `240ms cubic-bezier(0.22, 0.78, 0.2, 1)` |
| **网格卡软壳变色** | `.ms-grid-card:hover` | `background: #F7F9FD → #F3F5FA`；`outline-color: #EFF2F9` | `200ms ease` |
| **卡片白底 Tag 微浮** | `.ms-card-tag:hover` | `background: #FFFFFF`；`box-shadow: 0 2px 12px rgba(150, 150, 176, 0.3)` | `200ms ease` |
| **精选卡悬停反馈** | `.ms-featured-card:hover` | `transform: translateY(-2px)` + 封面 `scale(1.05)` + 右上显影圆形 `↗` | `220ms cubic-bezier(0.22, 0.78, 0.2, 1)` |
| **渐变主按钮反馈** | `.ms-cta-btn:hover / :active` | Hover `opacity: 0.90` + 箭头微移 `translate(1px,-1px)`；Active `scale(0.97)` | `140ms cubic-bezier(0.22, 0.78, 0.2, 1)` |
| **Hero 搜索框聚焦** | `.ms-hero-search:focus-within` | `border-color: #ECEDF1 → #624AFF`；`box-shadow: 0 6px 22px rgba(98, 74, 255, 0.11)` | `200ms ease` |
| **顶栏导航激活弹性** | `.nav-tab.active` | `@keyframes msNavPop`: `scale(0.86) → scale(1)` | `325ms cubic-bezier(0.2727, 0.0986, 0.8333, 1)` |
| **星轨热榜轮播切换** | 定时轮播 / 点击侧翼卡片 | `transform, width, height, opacity` 沿椭圆轨迹平滑插值 | `920ms–1000ms cubic-bezier(0.4, 0, 0.2, 1)` |
| **热榜拱心「去体验」显影** | 悬停热榜中心区域 | 统计行 `opacity: 0`，按钮 `opacity: 1; translateY(0)` | `180ms ease` |
| **骨架屏流光加载** | `.ms-skeleton` | `linear-gradient(90deg, #F3F5FA 25%, #EFF2F9 37%, #F3F5FA 63%)` `400%` 位移 | `1.4s ease infinite` |
