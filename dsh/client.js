// 浏览器端插件：左侧栏加一个「莉莉丝」图标，点开在主区域显示面板。
//
// 槽位来自《带左侧栏入口的 DSH 插件》文档（从线上 Slots 表核实）：
//   sidebar.panellist  list, root   侧边栏的全局面板图标，参数 id（必填，作为面板键）、order
//   main               keyed, root   主区域面板，key 必须与侧边栏条目 id 一致
//
// 之前用 conversation.view 是错的：那个槽位不是左侧导航，注册成功了也不会出现在那里
// （信标里 register 返回了清理函数，说明调用成功，只是位置不对）。
//
// 面板内容仍复用 host 半的 /dsh-companion/ui（同 origin），iframe 挂载。

window.__ModuleLoader__.load({
  id: 'dsh-plugin-companion',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    const beacon = (stage, detail) => {
      try {
        const q = new URLSearchParams({ stage, detail: String(detail || '').slice(0, 280) });
        const token = new URLSearchParams(location.search).get('token');
        if (token) q.set('token', token);
        fetch('/dsh-companion/beacon?' + q.toString(), { credentials: 'same-origin' }).catch(() => {});
      } catch (e) {
        /* 信标失败不影响插件 */
      }
    };

    // 侧栏图标：用 currentColor 跟随宿主主题，不写死颜色
    function PanelIcon() {
      return h(
        'svg',
        { viewBox: '0 0 24 24', width: 20, height: 20, 'aria-hidden': true },
        h('circle', {
          cx: 12, cy: 12, r: 8.6,
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, opacity: 0.9,
        }),
        h('circle', { cx: 12, cy: 9.6, r: 2.1, fill: 'currentColor' }),
        h('path', {
          d: 'M7.6 17.2c1.3-2.5 2.9-3.7 4.4-3.7s3.1 1.2 4.4 3.7',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round',
        }),
      );
    }

    // 主区域面板
    function PanelPage() {
      return h('iframe', {
        src: '/dsh-companion/ui',
        title: '莉莉丝',
        style: {
          width: '100%',
          height: '100%',
          border: '0',
          display: 'block',
          background: 'transparent',
        },
      });
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        try {
          ctx.slots.inject('sidebar.panellist', () =>
            ctx.slots.register(
              { name: 'sidebar.panellist', id: 'dsh-companion', order: 5 },
              () => h('span', { title: '莉莉丝' }, h(PanelIcon)),
            ),
          );
          beacon('apply', 'sidebar.panellist 已注册');
        } catch (e) {
          beacon('apply', 'sidebar.panellist 失败: ' + (e && e.message));
        }

        try {
          ctx.slots.inject('main', () =>
            ctx.slots.register(
              { name: 'main', key: 'dsh-companion' },
              PanelPage,
            ),
          );
          beacon('apply', 'main 已注册');
        } catch (e) {
          beacon('apply', 'main 失败: ' + (e && e.message));
        }
      },
    };
  },
});
