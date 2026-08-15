import type { Locale } from '@/store/settingsStore';
import zh from '../../messages/zh.json';

export const SUPPORTED_LOCALES: Locale[] = ['zh'];

export function getMessages(_locale: Locale | string = 'zh') {
    return zh;
}
