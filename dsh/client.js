// 浏览器端插件：在 DeepSeek Harness 的一级导航里加一个「莉莉丝」入口。
//
// 页面本体复用 host 半渲染好的 /dsh-companion/ui（同一 origin），
// 这里只用 iframe 把它挂进 conversation.view 插槽——
// 不做 TSX、不做构建，少一个会坏的环节。
//
// 已验证的前提（web profile 实测）：
//   1. package.json 的 dsh.client 写 { platform: 'web', inject: [] } 能被识别
//   2. exports["./client"] 指向本文件，模块系统会把它编进启动图的 combo URL
//   3. factory 返回 { name, inject, apply } 与 dsh-context 的产物结构一致

window.__ModuleLoader__.load({
  id: 'dsh-plugin-companion',
  factory: (require) => {
    const React = require('react');
    const h = React.createElement;

    const Panel = () =>
      h('iframe', {
        src: '/dsh-companion/ui',
        title: '莉莉丝',
        style: {
          width: '100%',
          height: '100%',
          border: '0',
          display: 'block',
          background: '#0b1020',
        },
      });

    return {
      name: 'dsh-plugin-companion-client',
      inject: ['slots'],
      apply(ctx) {
        ctx.effect(
          () =>
            ctx.slots.inject('conversation.view', () =>
              ctx.slots.register(
                {
                  name: 'conversation.view',
                  id: 'dsh-companion',
                  order: 60,
                  label: () => '莉莉丝',
                },
                () => h(Panel),
              ),
            ),
          'companion: panel view',
        );
      },
    };
  },
});
