function PageHeader() {
  return (
    <>
      <div className="px-8 pt-6 pb-4">
        <h2 className="text-lg font-medium text-neutral-600 mb-2">Screen 1 v3: Integration Hub</h2>
        <div className="flex items-end gap-2 mb-1">
          <h1 className="text-2xl font-bold text-neutral-700">Start here</h1>
          <span className="text-xl text-neutral-600 mb-[2px]">(fastest to value)</span>
        </div>
        <p className="text-neutral-600">Connect 1 to start discovery</p>
      </div>
      <div className="w-full h-px bg-neutral-300 my-2 px-8"></div>
    </>
  );
}

export default PageHeader;