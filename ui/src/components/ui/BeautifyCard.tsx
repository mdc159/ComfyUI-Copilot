interface IProps {
  children?: React.ReactNode;
  className?: string;
  borderClassName?: string;
}

const BeautifyCard: React.FC<IProps> = ({ children, className, borderClassName }) => {
  return (
    <div className='sticky w-full'>
      <div className={`relative copilot-theme-card ${className || ''}`}>
        <div className={`card-border ${borderClassName || ''}`}/>
        {
          children
        }
      </div>
    </div>
  );
};

export default BeautifyCard;
