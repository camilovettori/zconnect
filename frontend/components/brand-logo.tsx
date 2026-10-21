import Image from "next/image";

// 256 px square, padded copy of public/log.png (the original is 675x589 and ~0.5 MB).
const BRAND_MARK = "/zconnect-mark.png";

export function BrandLogo({
  width = 40,
  height = 40,
  className = "",
}: {
  width?: number;
  height?: number;
  className?: string;
}) {
  return (
    <div
      className={[
        "flex items-center justify-center overflow-hidden rounded-2xl bg-white",
        "shadow-[0_18px_40px_-24px_rgba(15,23,42,0.45)]",
        className,
      ].join(" ")}
      style={{ width, height }}
    >
      <Image src={BRAND_MARK} alt="Zconnect logo" width={width} height={height} className="h-auto w-auto object-contain" priority unoptimized />
    </div>
  );
}
