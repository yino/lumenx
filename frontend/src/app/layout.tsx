import "./globals.css";
import "@xyflow/react/dist/style.css";
import EnvConfigChecker from "@/components/EnvConfigChecker";
import { Providers } from "@/components/Providers";

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh" className="atelier-dark" suppressHydrationWarning>
      <head>
        <title>漫屿AIGC 创作平台</title>
        <meta name="description" content="AI 漫剧与视频创作平台" />
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var P=["atelier-dark","bridge-dark","brand-dark","atelier-light","brand-light"];var d=JSON.parse(localStorage.getItem("lumenx-settings")||"{}");var t=d.state&&d.state.theme;document.documentElement.className=P.indexOf(t)>=0?t:"atelier-dark";}catch(e){document.documentElement.className="atelier-dark";}})();`,
          }}
        />
      </head>
      <body className="font-sans bg-background text-foreground antialiased">
        <Providers>
          <EnvConfigChecker />
          {children}
        </Providers>
      </body>
    </html>
  );
}
