import { describe, it, expect } from 'vitest';
import { getMessages, SUPPORTED_LOCALES } from '@/lib/i18n';

describe('i18n configuration', () => {
    it('运行时只支持中文 locale', () => {
        expect(SUPPORTED_LOCALES).toEqual(['zh']);
    });

    it('getMessages returns messages for zh', () => {
        const messages = getMessages('zh');
        expect(messages).toBeDefined();
        expect(messages.common.save).toBe('保存');
        expect(messages.nav.workspace).toBe('工作区');
        expect(messages.settings.title).toBe('设置');
    });

    it('未知 locale 不会回退到英文目录', () => {
        const messages = getMessages('fr');
        expect(messages.common.save).toBe('保存');
    });
});
